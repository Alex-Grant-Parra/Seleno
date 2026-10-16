// Board tests and motor tools opened from the menu (ui_panels.cpp).
#pragma once

#include <functional>

#include "ui_internal.h"

namespace ui {

Screen* jogScreen();          // hold LEFT/RIGHT to turn the selected axis
Screen* driverScreen();       // live TMC2209 registers
Screen* selfTestScreen();     // one-page health check
Screen* buttonTestScreen();   // shows each key as it is pressed
Screen* ledTestScreen();      // switch each LED
Screen* displayTestScreen();  // colour bars, edges, gradients
Screen* imuScreen();          // live BNO085 orientation
Screen* i2cScreen();          // bus scan
Screen* linkScreen();         // Pi link and the Pi's status values

// The axis the Motors panel works on (0 = RA, 1 = Dec).
int& uiAxis();
const char* axisName(int axis);

// Jog speeds in logical steps/s (1600 per motor revolution by default),
// indexed by settings().jogSpeed.
constexpr int kJogSpeedCount = 3;
extern const float kJogSpeeds[kJogSpeedCount];
extern const char* const kJogSpeedNames[kJogSpeedCount];

// Start a front-panel move. Offers to release a latched emergency stop
// first and switches the driver on if needed. fn returns false with *error
// set when the move is refused (shown as a toast).
bool panelMove(int axis, std::function<bool(const char**)> fn);

// While the LED test runs, host LED commands are refused.
bool ledTestActive();

}  // namespace ui
