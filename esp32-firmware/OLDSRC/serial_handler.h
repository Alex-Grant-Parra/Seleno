#ifndef SERIAL_HANDLER_H
#define SERIAL_HANDLER_H

#include <Arduino.h>

// Serial communication management
extern String g_rxLine;

void handleSerial();
void initializeSerial();
bool beginDisplayBlit(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint32_t byteCount);
// Begin a buffered display blit: allocate RAM to hold the incoming payload and
// return true if the transfer can start. The actual LCD write is deferred
// until the full payload is received, then handleSerial will perform a single
// bulk SPI write which avoids progressive on-screen updating.
bool beginDisplayBlitBuffered(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint32_t byteCount);

// Begin uploading a file to persistent storage (LittleFS). The binary
// payload will be sent immediately after the command and written to the
// named file. Returns true if the upload can start.
bool beginFileUpload(const char* name, uint32_t byteCount);

#endif // SERIAL_HANDLER_H
