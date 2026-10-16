// Front panel: six buttons and five LEDs on the MCP23017 (U1, 0x20), plus
// the "board" LED on IO2. A dedicated FreeRTOS task owns the expander, so
// the RETURN-button emergency stop works even while the main loop is busy.
//
// Button events go to the host as {"event":"button", ...}.
#pragma once

#include <ArduinoJson.h>
#include <cstdint>

enum class Button : uint8_t { Select, Return, Top, Bottom, Left, Right, Count };

const char* buttonName(Button b);

void panelBegin();
bool panelExpanderPresent();
// Re-run the MCP23017 setup (e.g. after reconnecting it).
bool panelReinit();
// Bit n set = Button(n) held, after debouncing.
uint16_t panelPressedButtons();
void panelStatusJson(JsonObject out);

// LEDs: "board", "yellow", "blue", "white", "green", "red".
constexpr int kLedCount = 6;
int ledFind(const char* name);
const char* ledName(int led);
void ledSet(int led, bool on);
void ledToggle(int led);
void ledBlink(int led, uint32_t intervalMs, uint32_t autoOffMs);
bool ledState(int led);
