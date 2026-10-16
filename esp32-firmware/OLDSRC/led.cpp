#include "led.h"

LedChannel g_leds[kLedCount] = {
  {"board", 2, false, false, kDefaultLedBlinkMs, 0, 0},
  {"yellow", 18, false, false, kDefaultLedBlinkMs, 0, 0},
  {"blue", 5, false, false, kDefaultLedBlinkMs, 0, 0},
  {"white", 17, false, false, kDefaultLedBlinkMs, 0, 0},
  {"green", 16, false, false, kDefaultLedBlinkMs, 0, 0},
  {"red", 4, false, false, kDefaultLedBlinkMs, 0, 0},
};

LedChannel* findLed(const String& id) {
  for (size_t i = 0; i < kLedCount; i++) {
    if (g_leds[i].name != nullptr && id == g_leds[i].name) {
      return &g_leds[i];
    }
  }
  return nullptr;
}

void applyLedState(LedChannel* led, bool on) {
  if (led == nullptr) {
    return;
  }
  led->blinkEnabled = false;
  led->state = on;
  led->autoOffAtMs = 0;
}

void startLedBlink(LedChannel* led, uint32_t intervalMs, uint32_t autoOffMs) {
  if (led == nullptr) {
    return;
  }
  led->blinkEnabled = true;
  led->blinkIntervalMs = intervalMs == 0 ? kDefaultLedBlinkMs : intervalMs;
  led->lastToggleMs = millis();
  led->autoOffAtMs = autoOffMs > 0 ? (led->lastToggleMs + autoOffMs) : 0;
}

void initializeLeds() {
  for (size_t i = 0; i < kLedCount; i++) {
    pinMode(g_leds[i].pin, OUTPUT);
    digitalWrite(g_leds[i].pin, LOW);
  }
}

void updateLedStates() {
  uint32_t now = millis();
  for (size_t i = 0; i < kLedCount; i++) {
    LedChannel& led = g_leds[i];
    if (led.blinkEnabled) {
      if (led.autoOffAtMs != 0 && (int32_t)(now - led.autoOffAtMs) >= 0) {
        led.blinkEnabled = false;
        led.state = false;
        led.autoOffAtMs = 0;
      } else if (now - led.lastToggleMs >= led.blinkIntervalMs) {
        led.state = !led.state;
        led.lastToggleMs = now;
      }
    }
    digitalWrite(led.pin, led.state ? HIGH : LOW);
  }
}
