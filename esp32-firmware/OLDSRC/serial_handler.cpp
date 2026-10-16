#include "serial_handler.h"
#include "commands.h"
#include "display.h"
#include <LittleFS.h>
#include <cstring>

String g_rxLine;
static bool g_displayBlitActive = false;
static bool g_displayBlitBuffered = false;
static uint32_t g_displayBlitRemaining = 0;
// Buffer for streaming mode (small fixed chunk)
static uint8_t g_displayStreamBuffer[256];
// Pointer-based buffer for buffered (full-frame) mode
static uint8_t* g_displayBlitBuffer = nullptr;
static uint32_t g_displayBlitIndex = 0;
static uint16_t g_displayBlitX = 0;
static uint16_t g_displayBlitY = 0;
static uint16_t g_displayBlitW = 0;
static uint16_t g_displayBlitH = 0;
static uint32_t g_displayBlitLastByteMs = 0;
// File upload state
static bool g_fileUploadActive = false;
static uint32_t g_fileUploadRemaining = 0;
static uint8_t* g_fileUploadBuffer = nullptr;
static uint32_t g_fileUploadIndex = 0;
static uint32_t g_fileUploadLastByteMs = 0;
static char g_fileUploadName[64];

// Abort a binary transfer if the host goes quiet for this long, so the command
// parser can recover without requiring a reboot.
static constexpr uint32_t kTransferTimeoutMs = 5000U;

static String normalizeFsPath(const char* name) {
  String path(name == nullptr ? "" : name);
  path.trim();
  if (path.length() > 0 && path[0] != '/') {
    path = "/" + path;
  }
  return path;
}

bool beginDisplayBlit(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint32_t byteCount) {
  // Backwards-compatible existing behavior: start immediate streaming blit if
  // no buffered mode is used. This keeps legacy callers working.
  if (g_displayBlitActive || byteCount == 0) {
    return false;
  }

  displayBeginBlit(x, y, w, h);
  g_displayBlitActive = true;
  g_displayBlitRemaining = byteCount;
  g_displayBlitLastByteMs = millis();
  return true;
}

bool beginDisplayBlitBuffered(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint32_t byteCount) {
  if (g_displayBlitActive || g_displayBlitBuffered || byteCount == 0) {
    return false;
  }

  // Try to allocate a buffer for the incoming frame. If allocation fails,
  // fall back to streaming mode by returning false.
  uint8_t* buf = (uint8_t*)malloc(byteCount);
  if (buf == nullptr) {
    return false;
  }

  g_displayBlitBuffer = buf;
  g_displayBlitIndex = 0;
  g_displayBlitRemaining = byteCount;
  g_displayBlitBuffered = true;
  g_displayBlitX = x;
  g_displayBlitY = y;
  g_displayBlitW = w;
  g_displayBlitH = h;
  g_displayBlitLastByteMs = millis();
  return true;
}

bool beginFileUpload(const char* name, uint32_t byteCount) {
  if (g_fileUploadActive || byteCount == 0 || name == nullptr) {
    return false;
  }

  // Try to open the file for writing (overwrite existing)
  String fname = normalizeFsPath(name);
  if (fname.length() == 0 || fname.length() >= sizeof(g_fileUploadName)) {
    return false;
  }

  // Allocate a RAM buffer for fast serial ingest; file I/O happens after the
  // full payload is received to avoid UART overruns while LittleFS writes.
  uint8_t* uploadBuf = (uint8_t*)malloc(byteCount);
  if (uploadBuf == nullptr) {
    return false;
  }

  // Save state
  strncpy(g_fileUploadName, fname.c_str(), sizeof(g_fileUploadName) - 1);
  g_fileUploadName[sizeof(g_fileUploadName) - 1] = '\0';
  g_fileUploadBuffer = uploadBuf;
  g_fileUploadIndex = 0;
  g_fileUploadRemaining = byteCount;
  g_fileUploadLastByteMs = millis();
  g_fileUploadActive = true;
  return true;
}

void initializeSerial() {
  // 921600 is significantly more stable than 2M on many USB-UART bridges
  // during sustained binary transfers.
  Serial.begin(921600);
}

void handleSerial() {
  uint32_t now = millis();
  if (g_fileUploadActive) {
    if ((now - g_fileUploadLastByteMs) > kTransferTimeoutMs) {
      // Abort stale upload sessions (for example, host interrupted mid-transfer)
      free(g_fileUploadBuffer);
      g_fileUploadBuffer = nullptr;
      g_fileUploadActive = false;
      g_fileUploadIndex = 0;
      g_fileUploadRemaining = 0;
      g_fileUploadName[0] = '\0';
      sendError("Upload timeout");
    }
  }

  if ((g_displayBlitBuffered || g_displayBlitActive) &&
      (now - g_displayBlitLastByteMs) > kTransferTimeoutMs) {
    // Without this, an interrupted blit would swallow every later command as
    // pixel data.
    if (g_displayBlitActive) {
      displayEndBlit();
    }
    free(g_displayBlitBuffer);
    g_displayBlitBuffer = nullptr;
    g_displayBlitBuffered = false;
    g_displayBlitActive = false;
    g_displayBlitIndex = 0;
    g_displayBlitRemaining = 0;
    sendError("Blit timeout");
  }

  while (Serial.available() > 0) {
    if (g_fileUploadActive) {
      size_t toRead = Serial.available();
      if (toRead > sizeof(g_displayStreamBuffer)) {
        toRead = sizeof(g_displayStreamBuffer);
      }
      if (toRead > g_fileUploadRemaining) {
        toRead = g_fileUploadRemaining;
      }

      size_t readCount = Serial.readBytes(reinterpret_cast<char*>(g_displayStreamBuffer), toRead);
      if (readCount == 0) {
        return;
      }

      g_fileUploadLastByteMs = millis();

      if (g_fileUploadBuffer != nullptr) {
        memcpy(g_fileUploadBuffer + g_fileUploadIndex, g_displayStreamBuffer, readCount);
      }
      g_fileUploadIndex += static_cast<uint32_t>(readCount);

      g_fileUploadRemaining -= static_cast<uint32_t>(readCount);

      if (g_fileUploadRemaining == 0) {
        bool writeOk = false;
        if (g_fileUploadBuffer != nullptr && g_fileUploadName[0] != '\0') {
          if (LittleFS.begin(true)) {
            String fname(g_fileUploadName);
            if (LittleFS.exists(fname)) {
              LittleFS.remove(fname);
            }

            File f = LittleFS.open(fname, FILE_WRITE);
            if (f) {
              uint32_t written = 0;
              while (written < g_fileUploadIndex) {
                size_t toWrite = g_fileUploadIndex - written;
                if (toWrite > 4096) {
                  toWrite = 4096;
                }
                size_t w = f.write(g_fileUploadBuffer + written, toWrite);
                if (w == 0) {
                  break;
                }
                written += static_cast<uint32_t>(w);
                yield();
              }
              f.close();
              writeOk = (written == g_fileUploadIndex);
            }
          }
        }

        free(g_fileUploadBuffer);
        g_fileUploadBuffer = nullptr;
        g_fileUploadActive = false;
        g_fileUploadIndex = 0;
        g_fileUploadRemaining = 0;
        g_fileUploadLastByteMs = 0;
        g_fileUploadName[0] = '\0';

        if (!writeOk) {
          sendError("File write failed");
          return;
        }
        sendOkEmpty();
        return;
      }
      continue;
    }
    if (g_displayBlitBuffered) {
      // Read into the allocated buffer until full, then perform a single
      // bulk SPI write to update the display atomically.
      size_t toRead = Serial.available();
      if (toRead > g_displayBlitRemaining) {
        toRead = g_displayBlitRemaining;
      }

      size_t readCount = Serial.readBytes(reinterpret_cast<char*>(g_displayBlitBuffer + g_displayBlitIndex), toRead);
      if (readCount == 0) {
        return;
      }

      g_displayBlitLastByteMs = millis();
      g_displayBlitIndex += static_cast<uint32_t>(readCount);
      g_displayBlitRemaining -= static_cast<uint32_t>(readCount);

      if (g_displayBlitRemaining == 0) {
        // Now that we've fully received the frame, perform a single bulk
        // SPI write: set window, RAMWR, then write all bytes at once.
        displayBeginBlit(g_displayBlitX, g_displayBlitY, g_displayBlitW, g_displayBlitH);
        // Use the public API to write the received blit buffer in one go.
        displayWriteBlitData(g_displayBlitBuffer, g_displayBlitIndex);
        displayEndBlit();

        // Free buffer and clear state
        free(g_displayBlitBuffer);
        g_displayBlitBuffer = nullptr;
        g_displayBlitBuffered = false;
        g_displayBlitIndex = 0;
        g_displayBlitRemaining = 0;

        sendOkEmpty();
        return;
      }
      continue;
    }

    if (g_displayBlitActive) {
      size_t toRead = Serial.available();
      if (toRead > sizeof(g_displayStreamBuffer)) {
        toRead = sizeof(g_displayStreamBuffer);
      }
      if (toRead > g_displayBlitRemaining) {
        toRead = g_displayBlitRemaining;
      }

      size_t readCount = Serial.readBytes(reinterpret_cast<char*>(g_displayStreamBuffer), toRead);
      if (readCount == 0) {
        return;
      }

      g_displayBlitLastByteMs = millis();
      displayWriteBlitData(g_displayStreamBuffer, readCount);
      g_displayBlitRemaining -= static_cast<uint32_t>(readCount);

      if (g_displayBlitRemaining == 0) {
        g_displayBlitActive = false;
        displayEndBlit();
        sendOkEmpty();
        return;
      }
      continue;
    }

    char c = static_cast<char>(Serial.read());
    if (c == '\n') {
      handleCommand(g_rxLine);
      g_rxLine = "";
    } else if (c != '\r') {
      // Allow larger command lines to support CSV lists for sequence playback
      // (increase from 256 to 4096 characters).
      if (g_rxLine.length() < 4096) {
        g_rxLine += c;
      }
    }
  }
}
