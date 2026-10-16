// RGB888 hex strings <-> RGB565.
#pragma once

#include <cstdint>
#include <cstdio>
#include <cstdlib>

namespace seleno {

inline uint16_t rgb888ToRgb565(uint32_t rgb) {
  uint16_t r5 = ((rgb >> 16) & 0xFF) >> 3;
  uint16_t g6 = ((rgb >> 8) & 0xFF) >> 2;
  uint16_t b5 = (rgb & 0xFF) >> 3;
  return static_cast<uint16_t>((r5 << 11) | (g6 << 5) | b5);
}

// Accepts "RRGGBB" with or without a leading '#'. Unparseable input is black.
inline uint16_t hexToRgb565(const char* hex) {
  if (hex == nullptr) {
    return 0;
  }
  if (*hex == '#') {
    hex++;
  }
  return rgb888ToRgb565(static_cast<uint32_t>(strtoul(hex, nullptr, 16)));
}

inline uint32_t rgb565ToRgb888(uint16_t color) {
  uint32_t r5 = (color >> 11) & 0x1F;
  uint32_t g6 = (color >> 5) & 0x3F;
  uint32_t b5 = color & 0x1F;
  uint32_t r = (r5 << 3) | (r5 >> 2);
  uint32_t g = (g6 << 2) | (g6 >> 4);
  uint32_t b = (b5 << 3) | (b5 >> 2);
  return (r << 16) | (g << 8) | b;
}

// out must hold at least 7 chars.
inline void rgb565ToHex(uint16_t color, char* out) {
  snprintf(out, 7, "%06lX", static_cast<unsigned long>(rgb565ToRgb888(color)));
}

}  // namespace seleno
