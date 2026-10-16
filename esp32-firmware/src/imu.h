// BNO085 IMU on the separate IMU board (J3, I2C 0x4A or 0x4B), through the
// Adafruit_BNO08x library.
//
// Runs in its own low-priority task, so a slow or misbehaving IMU (the
// BNO085 stretches the I2C clock) can't hold up the front panel. The IMU
// board can be plugged in later; the task keeps looking for it.
#pragma once

#include <ArduinoJson.h>

void imuBegin();
bool imuPresent();
// Turn polling on or off. Off releases the bus completely.
void imuSetEnabled(bool on);
bool imuEnabled();
// Forget the current device and look for it again.
void imuRequestReinit();
void imuStatusJson(JsonObject out);
// Latest readings, with their age in ms. Empty if nothing has arrived yet.
void imuReadingJson(JsonObject out);
