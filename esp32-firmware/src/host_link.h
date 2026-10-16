// Host link to the Pi: newline-delimited JSON commands in, JSON responses
// and events out, plus raw binary payloads for display transfers.
//
// Ports:
//   0  UART0 (Serial). The v0.8 board wires it to the Pi's GPIO14/15 via J7;
//      on the dev kit it is the USB (CH340) port.
//   1  Auxiliary UART (Serial1, RX IO26 / TX IO25), only when built with
//      SELENO_AUX_HOST_UART=1 (the devkit env). Lets the Pi's GPIO UART talk
//      to a dev kit over jumpers while USB stays connected.
// A response goes back to the port its command came from. Events go to
// every port that has sent a command (port 0 always).
//
// Only the main loop task writes to the ports. Other tasks hand events to
// hostEmitEvent(), which queues them for the main loop.
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

constexpr int kMaxHostPorts = 2;

// Where a response must go: the request's port and its "id" member, kept as
// serialized JSON (a number or a string) so it can be echoed verbatim. The
// id is empty when the request had none (older hosts).
struct RequestId {
  char json[40] = {0};
  int8_t port = 0;
  bool present() const { return json[0] != '\0'; }
};

// The command being handled right now.
const RequestId& hostCurrentRequestId();
// Stream for the response to the current command (for raw streaming).
Print& hostOut();

void hostBegin();
// Reads and dispatches everything waiting on the ports, then writes queued
// events. Call from loop().
void hostPoll();
// Write queued events now. Long-running commands call this so events keep
// flowing while they work.
void hostFlushEvents();
const char* hostPortName(int port);
// TX pacing for every host port (see config.h). chunkBytes 0 turns it off.
void hostSetTxPacing(uint16_t chunkBytes, uint16_t gapUs);
void hostTxPacing(uint16_t& chunkBytes, uint16_t& gapUs);
int hostPortCount();
// millis() of the last byte received on any host port (0 = never).
uint32_t hostLastRxMs();

// Responses to the current command.
void hostSendOk();
void hostSendError(const char* message);
// Fills in status and id, then writes doc. Put the payload in doc["data"].
void hostSendResponse(JsonDocument& doc);
// For responses that complete after a binary payload (blit, store).
void hostSendOkFor(const RequestId& id);
void hostSendErrorFor(const RequestId& id, const char* message);

// Raw streamed response for large payloads: writes
// {"status":"ok",["id":..,]"data": and the caller prints the data value to
// hostOut(), then calls hostEndRawResponse().
void hostBeginRawResponse();
void hostEndRawResponse();
// Print s as the inside of a JSON string literal.
void hostPrintJsonEscaped(Print& out, const char* s);

// Queue an unsolicited event line. doc must contain "event". Safe from any
// task; never blocks. Returns false if the queue was full (event dropped).
bool hostEmitEvent(JsonDocument& doc);
uint32_t hostDroppedEventCount();

// Binary payloads that follow a JSON header command.
enum class BinaryKind : uint8_t {
  None,
  BlitBuffered,  // whole RGB565 frame into RAM, then one panel write
  BlitStream,    // RGB565 straight to the panel in chunks (low-memory fallback)
  BlitZlib,      // zlib-compressed RGB565: buffer, inflate, then one write
  FileUpload,    // bytes into RAM, then written to LittleFS
  SkyScene,      // a projected sky scene for the UI (ui_sky.h)
};

struct BinaryTransfer {
  BinaryKind kind = BinaryKind::None;
  RequestId id;            // also says which port the payload arrives on
  uint32_t total = 0;      // bytes expected on the wire
  uint32_t received = 0;
  uint8_t* buffer = nullptr;  // malloc'd for buffered kinds, freed by host_link
  uint32_t lastByteMs = 0;
  uint16_t x = 0, y = 0, w = 0, h = 0;
  uint32_t rawLength = 0;  // decompressed size for BlitZlib
  char name[64] = {0};     // file name for FileUpload
};

// Start receiving a payload on the current command's port. For buffered
// kinds this allocates t.total bytes; returns false if that fails or a
// transfer is already running.
bool hostBeginBinary(const BinaryTransfer& t);
bool hostBinaryActive();

// Implemented by the display commands.
void onBinaryChunk(BinaryTransfer& t, const uint8_t* data, size_t len);
// Called once all bytes have arrived; must send the deferred response.
void onBinaryComplete(BinaryTransfer& t);
// Called when the host stops sending mid-transfer.
void onBinaryAborted(BinaryTransfer& t);

// Implemented by commands.cpp.
void handleCommandLine(char* line, size_t len);
