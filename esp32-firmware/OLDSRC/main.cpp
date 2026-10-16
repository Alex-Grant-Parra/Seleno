#include <Arduino.h>

#include "led.h"
#include "display.h"
#include "serial_handler.h"
#include <LittleFS.h>

void setup() {
  initializeSerial();
  initializeLeds();
  // Display initialization is deferred - called via serial command
  // This prevents boot issues if display hardware has problems
  // Ensure filesystem is mounted for storing display assets
  LittleFS.begin(true);
}

void loop() {
  handleSerial();
  updateLedStates();
}
