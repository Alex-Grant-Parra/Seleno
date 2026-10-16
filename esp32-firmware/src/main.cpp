// SELENO v0.8 controller firmware. See ../PROTOCOL.md for the host protocol
// and "SELENO Controller v0.8 Hardware Report.txt" for the hardware.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <esp_system.h>

#include "board_pins.h"
#include "config.h"
#include "front_panel.h"
#include "host_link.h"
#include "i2c_bus.h"
#include "imu.h"
#include "motors.h"
#include "storage.h"
#include "tmc_drivers.h"
#include "ui.h"

// ArduinoJson documents, the display path and LittleFS share the loop
// task's stack; 8 KB is the default.
SET_LOOP_TASK_STACK_SIZE(16 * 1024);

void setup() {
  hostBegin();

  // GPIO5 is a strap with an internal pull-up: hold the LCD backlight off
  // until the panel is initialised and cleared.
  pinMode(PIN_LCD_BL, OUTPUT);
  digitalWrite(PIN_LCD_BL, LOW);

  // Before anything else touches Wire (see i2c_bus.cpp).
  i2cBegin();
  storageBegin();

  // Drivers first: ENN high, then configure over UART. Then the step timer.
  driversBegin();
  motorsBegin();

  panelBegin();
  imuBegin();
  // Loading screen until the Pi says hello (see ui.h).
  uiBegin();

  JsonDocument ev;
  ev["event"] = "boot";
  ev["fw"] = SELENO_FW_VERSION;
  ev["board"] = SELENO_BOARD_NAME;
  ev["devkit"] = SELENO_DEVKIT != 0;
  ev["reset_reason"] = static_cast<int>(esp_reset_reason());
  JsonArray drivers = ev["drivers"].to<JsonArray>();
  for (int i = 0; i < kAxisCount; i++) {
    drivers.add(driverPresent(i));
  }
  ev["expander"] = panelExpanderPresent();
  ev["ui"] = true;
  hostEmitEvent(ev);
  hostFlushEvents();
}

void loop() {
  hostPoll();
  driversPoll();
  motorsPoll();
  uiPoll();
  hostFlushEvents();
  // Let the idle task run (task watchdog) without adding real latency.
  delay(1);
}
