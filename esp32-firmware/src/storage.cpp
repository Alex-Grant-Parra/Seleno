#include "storage.h"

#include <LittleFS.h>

namespace {
bool g_mounted = false;
}

bool storageBegin() {
  g_mounted = LittleFS.begin(true);
  return g_mounted;
}

bool storageMounted() {
  return g_mounted;
}

String storagePath(const char* name) {
  String path(name == nullptr ? "" : name);
  path.trim();
  if (path.length() > 0 && path[0] != '/') {
    path = "/" + path;
  }
  return path;
}

bool storageWriteFile(const char* name, const uint8_t* data, size_t len) {
  if (!g_mounted || data == nullptr) {
    return false;
  }
  String path = storagePath(name);
  if (path.length() <= 1) {
    return false;
  }
  if (LittleFS.exists(path)) {
    LittleFS.remove(path);
  }
  File f = LittleFS.open(path, FILE_WRITE);
  if (!f) {
    return false;
  }
  size_t written = 0;
  while (written < len) {
    size_t chunk = len - written;
    if (chunk > 4096) {
      chunk = 4096;
    }
    size_t w = f.write(data + written, chunk);
    if (w == 0) {
      break;
    }
    written += w;
    yield();
  }
  f.close();
  return written == len;
}
