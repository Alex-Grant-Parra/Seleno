#include "host_link.h"

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#include "board_pins.h"
#include "config.h"

namespace {

constexpr size_t kEventLineBytes = 256;
constexpr size_t kEventQueueDepth = 32;

struct EventLine {
  char text[kEventLineBytes];
};

uint16_t g_txChunk = kHostTxChunkBytes;
uint16_t g_txGapUs = kHostTxGapUs;

// Print adapter that never lets more than g_txChunk bytes go out back to
// back: after each chunk it waits for the UART to drain, then idles
// g_txGapUs, so a USB-serial bridge with a small FIFO keeps up.
class PacedOut : public Print {
 public:
  void attach(HardwareSerial* serial) { serial_ = serial; }

  size_t write(uint8_t c) override { return write(&c, 1); }

  size_t write(const uint8_t* buf, size_t len) override {
    if (g_txChunk == 0) {
      return serial_->write(buf, len);
    }
    // Quiet for longer than a gap: the bridge has caught up.
    if (burst_ > 0 && micros() - lastWriteUs_ > static_cast<uint32_t>(g_txGapUs) + 2000u) {
      burst_ = 0;
    }
    size_t done = 0;
    while (done < len) {
      size_t room = g_txChunk > burst_ ? g_txChunk - burst_ : 0;
      size_t n = len - done < room ? len - done : room;
      if (n > 0) {
        serial_->write(buf + done, n);
        done += n;
        burst_ += n;
      }
      if (burst_ >= g_txChunk) {
        serial_->flush();
        delayMicroseconds(g_txGapUs);
        burst_ = 0;
      }
    }
    lastWriteUs_ = micros();
    return len;
  }

  void flush() override { serial_->flush(); }

 private:
  HardwareSerial* serial_ = nullptr;
  size_t burst_ = 0;
  uint32_t lastWriteUs_ = 0;
};

struct HostPort {
  HardwareSerial* serial;
  PacedOut out;
  const char* name;
  bool active;  // has sent a command, so it gets events
  char line[kMaxCommandLineBytes + 1];
  size_t lineLen;
  bool lineOverflow;
};

HostPort g_ports[kMaxHostPorts];
int g_portCount = 0;

QueueHandle_t g_eventQueue = nullptr;
volatile uint32_t g_droppedEvents = 0;

RequestId g_currentId;

BinaryTransfer g_transfer;
uint8_t g_chunk[512];

Print& portOut(int port) {
  if (port < 0 || port >= g_portCount) {
    port = 0;
  }
  return g_ports[port].out;
}

void writeIdMember(Print& out, const RequestId& id) {
  if (id.present()) {
    out.print("\"id\":");
    out.print(id.json);
    out.print(',');
  }
}

void resetTransfer() {
  free(g_transfer.buffer);
  g_transfer = BinaryTransfer();
}

// Pull the "id" member out of a command line without a full parse, so even
// a command that fails to parse can echo it. Accepts numbers and strings.
void extractRequestId(const char* line, RequestId& out) {
  out.json[0] = '\0';
  const char* p = strstr(line, "\"id\"");
  if (p == nullptr) {
    return;
  }
  p += 4;
  while (*p == ' ' || *p == '\t') p++;
  if (*p != ':') {
    return;
  }
  p++;
  while (*p == ' ' || *p == '\t') p++;
  size_t n = 0;
  if (*p == '"') {
    out.json[n++] = *p++;
    while (*p != '\0' && *p != '"' && n < sizeof(out.json) - 2) {
      if (*p == '\\') {
        out.json[0] = '\0';  // escaped ids aren't worth supporting
        return;
      }
      out.json[n++] = *p++;
    }
    if (*p != '"') {
      out.json[0] = '\0';
      return;
    }
    out.json[n++] = '"';
  } else {
    while ((*p == '-' || (*p >= '0' && *p <= '9')) && n < sizeof(out.json) - 1) {
      out.json[n++] = *p++;
    }
  }
  out.json[n] = '\0';
}

void pollTransferTimeout() {
  if (g_transfer.kind == BinaryKind::None) {
    return;
  }
  if (millis() - g_transfer.lastByteMs <= kTransferTimeoutMs) {
    return;
  }
  // Without this an interrupted transfer would swallow every later command
  // on that port as payload.
  onBinaryAborted(g_transfer);
  RequestId id = g_transfer.id;
  BinaryKind kind = g_transfer.kind;
  resetTransfer();
  hostSendErrorFor(id, kind == BinaryKind::FileUpload ? "Upload timeout" : "Blit timeout");
}

// Returns true when the transfer finished, so the caller stops reading for
// this pass and the response goes out before the next command is parsed.
bool pumpTransfer(HardwareSerial& in) {
  while (in.available() > 0 && g_transfer.kind != BinaryKind::None) {
    uint32_t remaining = g_transfer.total - g_transfer.received;
    size_t avail = static_cast<size_t>(in.available());
    size_t toRead = avail < remaining ? avail : remaining;
    size_t got;
    if (g_transfer.buffer != nullptr) {
      got = in.readBytes(g_transfer.buffer + g_transfer.received, toRead);
    } else {
      if (toRead > sizeof(g_chunk)) {
        toRead = sizeof(g_chunk);
      }
      got = in.readBytes(g_chunk, toRead);
      if (got > 0) {
        onBinaryChunk(g_transfer, g_chunk, got);
      }
    }
    if (got == 0) {
      return false;
    }
    g_transfer.received += static_cast<uint32_t>(got);
    g_transfer.lastByteMs = millis();
    if (g_transfer.received >= g_transfer.total) {
      g_currentId = g_transfer.id;
      onBinaryComplete(g_transfer);
      resetTransfer();
      g_currentId = RequestId();
      return true;
    }
  }
  return false;
}

void pollPort(int index) {
  HostPort& port = g_ports[index];
  HardwareSerial& in = *port.serial;

  while (in.available() > 0) {
    if (g_transfer.kind != BinaryKind::None && g_transfer.id.port == index) {
      if (pumpTransfer(in)) {
        return;
      }
      continue;
    }

    int c = in.read();
    if (c < 0) {
      return;
    }
    if (c == '\n') {
      port.line[port.lineLen] = '\0';
      port.active = true;
      extractRequestId(port.line, g_currentId);
      g_currentId.port = static_cast<int8_t>(index);
      if (port.lineOverflow) {
        hostSendError("Command too long");
      } else if (port.lineLen > 0) {
        handleCommandLine(port.line, port.lineLen);
      }
      g_currentId = RequestId();
      port.lineLen = 0;
      port.lineOverflow = false;
      hostFlushEvents();
      // A binary header may have just started a transfer on this port; its
      // payload is read on the next pass through this loop.
      continue;
    }
    if (c == '\r') {
      continue;
    }
    if (port.lineLen < kMaxCommandLineBytes) {
      port.line[port.lineLen++] = static_cast<char>(c);
    } else {
      port.lineOverflow = true;
    }
  }
}

void addPort(HardwareSerial* serial, const char* name, bool active) {
  HostPort& p = g_ports[g_portCount++];
  p.serial = serial;
  p.out.attach(serial);
  p.name = name;
  p.active = active;
  p.lineLen = 0;
  p.lineOverflow = false;
}

}  // namespace

const RequestId& hostCurrentRequestId() {
  return g_currentId;
}

Print& hostOut() {
  return portOut(g_currentId.port);
}

void hostBegin() {
  Serial.setRxBufferSize(kHostRxBufferBytes);
  Serial.begin(kHostBaud);
  addPort(&Serial, "uart0", true);
#if SELENO_AUX_HOST_UART
  Serial1.setRxBufferSize(kHostRxBufferBytes);
  Serial1.begin(kHostBaud, SERIAL_8N1, PIN_AUX_HOST_RX, PIN_AUX_HOST_TX);
  addPort(&Serial1, "aux_uart", false);
#endif
  g_eventQueue = xQueueCreate(kEventQueueDepth, sizeof(EventLine));
}

void hostPoll() {
  pollTransferTimeout();
  for (int i = 0; i < g_portCount; i++) {
    pollPort(i);
  }
  hostFlushEvents();
}

void hostSetTxPacing(uint16_t chunkBytes, uint16_t gapUs) {
  g_txChunk = chunkBytes;
  g_txGapUs = gapUs;
}

void hostTxPacing(uint16_t& chunkBytes, uint16_t& gapUs) {
  chunkBytes = g_txChunk;
  gapUs = g_txGapUs;
}

const char* hostPortName(int port) {
  return (port >= 0 && port < g_portCount) ? g_ports[port].name : "";
}

int hostPortCount() {
  return g_portCount;
}

void hostFlushEvents() {
  if (g_eventQueue == nullptr) {
    return;
  }
  EventLine ev;
  while (xQueueReceive(g_eventQueue, &ev, 0) == pdTRUE) {
    for (int i = 0; i < g_portCount; i++) {
      if (g_ports[i].active) {
        g_ports[i].out.print(ev.text);
        g_ports[i].out.print('\n');
      }
    }
  }
}

bool hostEmitEvent(JsonDocument& doc) {
  if (g_eventQueue == nullptr) {
    return false;
  }
  EventLine ev;
  size_t n = serializeJson(doc, ev.text, sizeof(ev.text));
  if (n == 0 || n >= sizeof(ev.text) - 1) {
    g_droppedEvents++;
    return false;
  }
  if (xQueueSend(g_eventQueue, &ev, 0) != pdTRUE) {
    g_droppedEvents++;
    return false;
  }
  return true;
}

uint32_t hostDroppedEventCount() {
  return g_droppedEvents;
}

void hostSendResponse(JsonDocument& doc) {
  if (doc["status"].isNull()) {
    doc["status"] = "ok";
  }
  if (g_currentId.present()) {
    doc["id"] = serialized(g_currentId.json);
  }
  Print& out = hostOut();
  serializeJson(doc, out);
  out.print('\n');
}

void hostSendOk() {
  hostSendOkFor(g_currentId);
}

void hostSendError(const char* message) {
  hostSendErrorFor(g_currentId, message);
}

void hostSendOkFor(const RequestId& id) {
  Print& out = portOut(id.port);
  out.print("{\"status\":\"ok\"");
  if (id.present()) {
    out.print(",\"id\":");
    out.print(id.json);
  }
  out.print("}\n");
}

void hostSendErrorFor(const RequestId& id, const char* message) {
  Print& out = portOut(id.port);
  out.print("{\"status\":\"error\",");
  writeIdMember(out, id);
  out.print("\"message\":\"");
  hostPrintJsonEscaped(out, message);
  out.print("\"}\n");
}

void hostBeginRawResponse() {
  Print& out = hostOut();
  out.print("{\"status\":\"ok\",");
  writeIdMember(out, g_currentId);
  out.print("\"data\":");
}

void hostEndRawResponse() {
  hostOut().print("}\n");
}

void hostPrintJsonEscaped(Print& out, const char* s) {
  if (s == nullptr) {
    return;
  }
  for (const char* p = s; *p != '\0'; p++) {
    char c = *p;
    if (c == '"' || c == '\\') {
      out.print('\\');
      out.print(c);
    } else if (static_cast<unsigned char>(c) < 0x20) {
      char esc[7];
      snprintf(esc, sizeof(esc), "\\u%04X", static_cast<unsigned char>(c));
      out.print(esc);
    } else {
      out.print(c);
    }
  }
}

bool hostBeginBinary(const BinaryTransfer& t) {
  if (g_transfer.kind != BinaryKind::None || t.kind == BinaryKind::None || t.total == 0) {
    return false;
  }
  g_transfer = t;
  g_transfer.id = g_currentId;
  g_transfer.buffer = nullptr;
  g_transfer.received = 0;
  g_transfer.lastByteMs = millis();
  if (t.kind != BinaryKind::BlitStream) {
    g_transfer.buffer = static_cast<uint8_t*>(malloc(t.total));
    if (g_transfer.buffer == nullptr) {
      g_transfer = BinaryTransfer();
      return false;
    }
  }
  return true;
}

bool hostBinaryActive() {
  return g_transfer.kind != BinaryKind::None;
}
