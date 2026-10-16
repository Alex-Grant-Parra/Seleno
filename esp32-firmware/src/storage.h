// LittleFS on the "spiffs" partition: display assets uploaded by the Pi.
#pragma once

#include <Arduino.h>

// Mounts LittleFS, formatting it if it can't be mounted.
bool storageBegin();
bool storageMounted();
// "frame.rgb" -> "/frame.rgb"
String storagePath(const char* name);
bool storageWriteFile(const char* name, const uint8_t* data, size_t len);
