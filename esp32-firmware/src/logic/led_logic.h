// LED channel state: on, off, blink with optional auto-off.
#pragma once

#include <cstdint>

namespace seleno {

constexpr uint32_t kDefaultLedBlinkMs = 500;

struct LedLogic {
  bool state = false;
  bool blinkEnabled = false;
  uint32_t blinkIntervalMs = kDefaultLedBlinkMs;
  uint32_t lastToggleMs = 0;
  uint32_t autoOffAtMs = 0;  // 0 = never

  void set(bool on) {
    blinkEnabled = false;
    state = on;
    autoOffAtMs = 0;
  }

  void startBlink(uint32_t intervalMs, uint32_t autoOffMs, uint32_t nowMs) {
    blinkEnabled = true;
    blinkIntervalMs = intervalMs == 0 ? kDefaultLedBlinkMs : intervalMs;
    lastToggleMs = nowMs;
    autoOffAtMs = autoOffMs > 0 ? (nowMs + autoOffMs) : 0;
    if (autoOffAtMs == 0 && autoOffMs > 0) {
      autoOffAtMs = 1;  // nowMs + autoOffMs wrapped to exactly 0
    }
  }

  // Advance blinking; returns the level the LED should show now.
  bool update(uint32_t nowMs) {
    if (blinkEnabled) {
      if (autoOffAtMs != 0 && static_cast<int32_t>(nowMs - autoOffAtMs) >= 0) {
        blinkEnabled = false;
        state = false;
        autoOffAtMs = 0;
      } else if (nowMs - lastToggleMs >= blinkIntervalMs) {
        state = !state;
        lastToggleMs = nowMs;
      }
    }
    return state;
  }
};

}  // namespace seleno
