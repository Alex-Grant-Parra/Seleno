#ifndef LED_H
#define LED_H

#include <Arduino.h>
#include <cstddef>
#include <cstdint>

struct LedChannel {
  const char* name;
  int pin;
  bool state;
  bool blinkEnabled;
  uint32_t blinkIntervalMs;
  uint32_t lastToggleMs;
  uint32_t autoOffAtMs;
};

constexpr uint32_t kDefaultLedBlinkMs = 500;
constexpr size_t kLedCount = 6;

extern LedChannel g_leds[kLedCount];

LedChannel* findLed(const String& id);
void applyLedState(LedChannel* led, bool on);
void startLedBlink(LedChannel* led, uint32_t intervalMs, uint32_t autoOffMs);
void initializeLeds();
void updateLedStates();

#endif // LED_H
