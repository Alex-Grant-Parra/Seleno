// {"cmd":"display", "action": ...} and the binary payloads that follow blit
// and store commands.
#include <Arduino.h>
#include <LittleFS.h>
#include <esp32/rom/miniz.h>

#include "commands.h"
#include "display.h"
#include "host_link.h"
#include "logic/color.h"
#include "storage.h"
#include "ui.h"
#include "ui_sky.h"

namespace {

uint16_t colorArg(JsonDocument& req, const char* key, const char* fallbackHex) {
  return seleno::hexToRgb565(req[key] | fallbackHex);
}

// Delay for frame `index` from a comma-separated list of milliseconds;
// 100 ms when the entry is missing or empty. Negative values count as 0.
uint32_t frameDelayMs(const char* durationsCsv, int index) {
  uint32_t delayMs = 100;
  if (durationsCsv == nullptr || durationsCsv[0] == '\0') {
    return delayMs;
  }
  const char* p = durationsCsv;
  for (int i = 0; i < index; i++) {
    p = strchr(p, ',');
    if (p == nullptr) {
      return delayMs;
    }
    p++;
  }
  while (*p == ' ') p++;
  if (*p == ',' || *p == '\0') {
    return delayMs;
  }
  long v = strtol(p, nullptr, 10);
  return v < 0 ? 0 : static_cast<uint32_t>(v);
}

bool requireStorage() {
  if (!storageMounted()) {
    hostSendError("Storage mount failed");
    return false;
  }
  return true;
}

void playFrame(const char* name, uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint32_t delayMs) {
  displayPlayFile(name, x, y, w, h);
  // Keep button and motion events flowing during long animations.
  hostFlushEvents();
  delay(delayMs);
}

void sendStatus() {
  JsonDocument resp;
  displayStatusJson(resp["data"].to<JsonObject>());
  hostSendResponse(resp);
}

void handleBlit(JsonDocument& req) {
  uint16_t x = req["x"] | 0;
  uint16_t y = req["y"] | 0;
  uint16_t w = req["w"] | 0;
  uint16_t h = req["h"] | 0;
  const char* format = req["format"] | "RGB565";
  const char* compress = req["compress"] | "";
  uint32_t length = req["length"] | 0u;
  uint32_t frameBytes = static_cast<uint32_t>(w) * h * 2U;

  if (w == 0 || h == 0) {
    hostSendError("Invalid blit size");
    return;
  }
  // The payload goes straight into panel RAM and can't be clipped.
  if (static_cast<uint32_t>(x) + w > displayWidth() || static_cast<uint32_t>(y) + h > displayHeight()) {
    hostSendError("Blit out of bounds");
    return;
  }
  if (!displayInitialized()) {
    hostSendError("Display not initialized");
    return;
  }
  if (strcmp(format, "RGB565") != 0) {
    hostSendError("Unsupported blit format");
    return;
  }

  BinaryTransfer t;
  t.x = x;
  t.y = y;
  t.w = w;
  t.h = h;
  t.total = length;

  if (strcmp(compress, "zlib") == 0) {
    uint32_t raw = req["uncompressed_length"] | 0u;
    if (raw != frameBytes || length == 0) {
      hostSendError("Invalid blit length");
      return;
    }
    t.kind = BinaryKind::BlitZlib;
    t.rawLength = raw;
    if (!hostBeginBinary(t)) {
      hostSendError("Unable to start display blit");
    }
    return;  // the response follows the payload
  }
  if (compress[0] != '\0') {
    hostSendError("Unsupported compression");
    return;
  }
  if (length != frameBytes) {
    hostSendError("Invalid blit length");
    return;
  }
  // Prefer buffering the whole frame for one tear-free write; stream it if
  // there isn't enough free RAM.
  t.kind = BinaryKind::BlitBuffered;
  if (hostBeginBinary(t)) {
    return;
  }
  t.kind = BinaryKind::BlitStream;
  if (!hostBeginBinary(t)) {
    hostSendError("Unable to start display blit");
    return;
  }
  displayBeginStream(x, y, w, h);
}

void handleStore(JsonDocument& req, bool replyNow) {
  const char* name = req["name"] | "";
  uint32_t length = req["length"] | 0u;
  if (name[0] == '\0' || length == 0) {
    hostSendError("Missing name or length");
    return;
  }
  if (!requireStorage()) {
    return;
  }
  BinaryTransfer t;
  t.kind = BinaryKind::FileUpload;
  t.total = length;
  String path = storagePath(name);
  if (path.length() >= sizeof(t.name)) {
    hostSendError("Name too long");
    return;
  }
  strncpy(t.name, path.c_str(), sizeof(t.name) - 1);
  if (!hostBeginBinary(t)) {
    hostSendError("Unable to start file upload");
    return;
  }
  // store_begin acknowledges now and again once the file is written; store
  // only answers at the end.
  if (replyNow) {
    hostSendOk();
  }
}

void handleListFiles() {
  if (!requireStorage()) {
    return;
  }
  // Streamed, so a long frame list never has to fit in one JSON document.
  Print& out = hostOut();
  hostBeginRawResponse();
  out.print("{\"files\":[");
  bool first = true;
  File root = LittleFS.open("/");
  if (root) {
    File file = root.openNextFile();
    while (file) {
      if (!first) {
        out.print(',');
      }
      first = false;
      out.print("{\"name\":\"");
      hostPrintJsonEscaped(out, file.name());
      out.print("\",\"size\":");
      out.print(static_cast<uint32_t>(file.size()));
      out.print('}');
      file = root.openNextFile();
    }
    root.close();
  }
  out.print("]}");
  hostEndRawResponse();
}

void handleStorageInfo() {
  if (!requireStorage()) {
    return;
  }
  size_t total = LittleFS.totalBytes();
  size_t used = LittleFS.usedBytes();
  JsonDocument resp;
  JsonObject data = resp["data"].to<JsonObject>();
  data["total_bytes"] = static_cast<uint32_t>(total);
  data["used_bytes"] = static_cast<uint32_t>(used);
  data["free_bytes"] = static_cast<uint32_t>(total >= used ? total - used : 0);
  hostSendResponse(resp);
}

void handleDeleteFile(JsonDocument& req) {
  const char* name = req["name"] | "";
  if (name[0] == '\0') {
    hostSendError("Missing name");
    return;
  }
  if (!requireStorage()) {
    return;
  }
  String path = storagePath(name);
  if (!LittleFS.exists(path)) {
    hostSendError("File not found");
    return;
  }
  if (!LittleFS.remove(path)) {
    hostSendError("Delete failed");
    return;
  }
  hostSendOk();
}

void handlePlaySequence(JsonDocument& req) {
  const char* filesCsv = req["files_csv"] | "";
  if (filesCsv[0] == '\0') {
    hostSendError("Missing files_csv");
    return;
  }
  if (!requireStorage()) {
    return;
  }
  const char* durationsCsv = req["durations_csv"] | "";
  uint16_t x = req["x"] | 0;
  uint16_t y = req["y"] | 0;
  uint16_t w = req["w"] | displayWidth();
  uint16_t h = req["h"] | displayHeight();

  String list(filesCsv);
  int start = 0;
  int index = 0;
  while (start < static_cast<int>(list.length())) {
    int comma = list.indexOf(',', start);
    String name = comma < 0 ? list.substring(start) : list.substring(start, comma);
    name.trim();
    if (name.length() > 0) {
      playFrame(name.c_str(), x, y, w, h, frameDelayMs(durationsCsv, index));
      index++;
    }
    if (comma < 0) {
      break;
    }
    start = comma + 1;
  }
  hostSendOk();
}

void handlePlaySequencePrefix(JsonDocument& req) {
  const char* prefix = req["prefix"] | "";
  int start = req["start"] | 0;
  int count = req["count"] | 0;
  int pad = req["pad"] | 4;
  if (prefix[0] == '\0' || count <= 0) {
    hostSendError("Missing prefix or count");
    return;
  }
  if (!requireStorage()) {
    return;
  }
  const char* durationsCsv = req["durations_csv"] | "";
  uint16_t x = req["x"] | 0;
  uint16_t y = req["y"] | 0;
  uint16_t w = req["w"] | displayWidth();
  uint16_t h = req["h"] | displayHeight();

  for (int i = start; i < start + count; i++) {
    char name[128];
    snprintf(name, sizeof(name), "%s%0*d.rgb", prefix, pad, i);
    playFrame(name, x, y, w, h, frameDelayMs(durationsCsv, i - start));
  }
  hostSendOk();
}

// Inflate a zlib stream into exactly outLen bytes. tinfl_decompressor is
// about 11 KB, too big for the loop task's stack.
bool inflateZlib(const uint8_t* in, size_t inLen, uint8_t* out, size_t outLen) {
  tinfl_decompressor* d = static_cast<tinfl_decompressor*>(malloc(sizeof(tinfl_decompressor)));
  if (d == nullptr) {
    return false;
  }
  tinfl_init(d);
  size_t inBytes = inLen;
  size_t outBytes = outLen;
  tinfl_status status = tinfl_decompress(d, in, &inBytes, out, out, &outBytes,
                                         TINFL_FLAG_PARSE_ZLIB_HEADER | TINFL_FLAG_USING_NON_WRAPPING_OUTPUT_BUF);
  free(d);
  return status == TINFL_STATUS_DONE && outBytes == outLen;
}

}  // namespace

bool handleDisplayCommand(const char* cmd, JsonDocument& req) {
  if (strcmp(cmd, "display") != 0) {
    return false;
  }
  const char* action = req["action"] | "";
  if (action[0] == '\0') {
    hostSendError("Missing display action");
    return true;
  }

  // Anything that draws takes the screen from the front-panel UI until
  // RETURN or a ui command hands it back.
  static const char* const kNonDrawing[] = {"status",     "format_storage", "storage_info", "list_files",
                                            "delete_file", "store_begin",   "store",        "init",
                                            "power",       "backlight"};
  bool draws = true;
  for (const char* a : kNonDrawing) {
    if (strcmp(action, a) == 0) draws = false;
  }
  if (draws) {
    uiHostDrawing();
  }

  // Storage actions work without the panel.
  if (strcmp(action, "status") == 0) {
    sendStatus();
  } else if (strcmp(action, "format_storage") == 0) {
    if (!requireStorage()) return true;
    if (!LittleFS.format()) {
      hostSendError("Storage format failed");
      return true;
    }
    hostSendOk();
  } else if (strcmp(action, "storage_info") == 0) {
    handleStorageInfo();
  } else if (strcmp(action, "list_files") == 0) {
    handleListFiles();
  } else if (strcmp(action, "delete_file") == 0) {
    handleDeleteFile(req);
  } else if (strcmp(action, "store_begin") == 0) {
    handleStore(req, true);
  } else if (strcmp(action, "store") == 0) {
    handleStore(req, false);
  } else if (strcmp(action, "init") == 0) {
    if (!displayInit()) {
      hostSendError("Display init failed");
      return true;
    }
    hostSendOk();
  } else if (strcmp(action, "power") == 0) {
    displayPower(req["on"] | true);
    hostSendOk();
  } else if (strcmp(action, "backlight") == 0) {
    displayBacklight(req["brightness"] | 255);
    hostSendOk();
  } else if (strcmp(action, "rotation") == 0) {
    displaySetRotation(req["rotation"] | 0);
    sendStatus();
  } else if (strcmp(action, "clear") == 0) {
    displayFillScreen(colorArg(req, "color", "000000"));
    hostSendOk();
  } else if (strcmp(action, "fill_rect") == 0) {
    displayFillRect(req["x"] | 0, req["y"] | 0, req["w"] | 10, req["h"] | 10, colorArg(req, "color", "FFFFFF"));
    hostSendOk();
  } else if (strcmp(action, "draw_rect") == 0) {
    displayDrawRect(req["x"] | 0, req["y"] | 0, req["w"] | 10, req["h"] | 10, colorArg(req, "color", "FFFFFF"));
    hostSendOk();
  } else if (strcmp(action, "draw_line") == 0) {
    displayDrawLine(req["x0"] | 0, req["y0"] | 0, req["x1"] | 127, req["y1"] | 159, colorArg(req, "color", "FFFFFF"));
    hostSendOk();
  } else if (strcmp(action, "draw_circle") == 0) {
    displayDrawCircle(req["x"] | 64, req["y"] | 80, req["r"] | 10, colorArg(req, "color", "FFFFFF"));
    hostSendOk();
  } else if (strcmp(action, "fill_circle") == 0) {
    displayFillCircle(req["x"] | 64, req["y"] | 80, req["r"] | 10, colorArg(req, "color", "FFFFFF"));
    hostSendOk();
  } else if (strcmp(action, "draw_pixel") == 0) {
    displayDrawPixel(req["x"] | 0, req["y"] | 0, colorArg(req, "color", "FFFFFF"));
    hostSendOk();
  } else if (strcmp(action, "set_cursor") == 0) {
    displaySetCursor(req["x"] | 0, req["y"] | 0);
    hostSendOk();
  } else if (strcmp(action, "set_text_color") == 0) {
    displaySetTextColor(colorArg(req, "color", "FFFFFF"));
    hostSendOk();
  } else if (strcmp(action, "set_bg_color") == 0) {
    displaySetBackgroundColor(colorArg(req, "color", "000000"));
    hostSendOk();
  } else if (strcmp(action, "set_text_size") == 0) {
    displaySetTextSize(req["size"] | 1);
    hostSendOk();
  } else if (strcmp(action, "text") == 0 || strcmp(action, "print") == 0) {
    // Optional position, size and colours, then the text at the cursor.
    if (!req["x"].isNull() || !req["y"].isNull()) {
      displaySetCursor(req["x"] | 0, req["y"] | 0);
    }
    if (!req["size"].isNull()) displaySetTextSize(req["size"] | 1);
    if (!req["color"].isNull()) displaySetTextColor(colorArg(req, "color", "FFFFFF"));
    if (!req["bg"].isNull()) displaySetBackgroundColor(colorArg(req, "bg", "000000"));
    displayPrint(req["text"] | "");
    if (req["newline"] | false) displayPrint("\n");
    hostSendOk();
  } else if (strcmp(action, "blit") == 0) {
    handleBlit(req);
  } else if (strcmp(action, "play") == 0) {
    const char* name = req["name"] | "";
    if (name[0] == '\0') {
      hostSendError("Missing name");
      return true;
    }
    if (!displayPlayFile(name, req["x"] | 0, req["y"] | 0, req["w"] | displayWidth(), req["h"] | displayHeight())) {
      hostSendError("Play failed");
      return true;
    }
    hostSendOk();
  } else if (strcmp(action, "play_sequence") == 0) {
    handlePlaySequence(req);
  } else if (strcmp(action, "play_sequence_prefix") == 0) {
    handlePlaySequencePrefix(req);
  } else {
    hostSendError("Unknown display action");
  }
  return true;
}

void onBinaryChunk(BinaryTransfer& t, const uint8_t* data, size_t len) {
  if (t.kind == BinaryKind::BlitStream) {
    displayStreamBytes(data, len);
  }
}

void onBinaryComplete(BinaryTransfer& t) {
  switch (t.kind) {
    case BinaryKind::BlitBuffered:
      displayPushRgb565(t.x, t.y, t.w, t.h, t.buffer);
      hostSendOkFor(t.id);
      break;
    case BinaryKind::BlitStream:
      displayEndStream();
      hostSendOkFor(t.id);
      break;
    case BinaryKind::BlitZlib: {
      uint8_t* frame = static_cast<uint8_t*>(malloc(t.rawLength));
      if (frame == nullptr) {
        hostSendErrorFor(t.id, "Not enough memory to inflate");
        break;
      }
      if (!inflateZlib(t.buffer, t.total, frame, t.rawLength)) {
        free(frame);
        hostSendErrorFor(t.id, "Invalid zlib payload");
        break;
      }
      displayPushRgb565(t.x, t.y, t.w, t.h, frame);
      free(frame);
      hostSendOkFor(t.id);
      break;
    }
    case BinaryKind::SkyScene:
      uiSkySceneComplete(t);
      break;
    case BinaryKind::FileUpload:
      if (storageWriteFile(t.name, t.buffer, t.total)) {
        hostSendOkFor(t.id);
      } else {
        hostSendErrorFor(t.id, "File write failed");
      }
      break;
    default:
      break;
  }
}

void onBinaryAborted(BinaryTransfer& t) {
  if (t.kind == BinaryKind::BlitStream) {
    displayEndStream();
  }
}
