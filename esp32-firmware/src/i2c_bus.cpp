#include "i2c_bus.h"

#include <Arduino.h>
#include <Wire.h>

#include "board_pins.h"
#include "config.h"

namespace {
volatile uint32_t g_errors = 0;
}

void i2cBegin() {
  // Must run before any library touches Wire: a later Wire.begin() with no
  // arguments is then a no-op instead of claiming IO21/IO22 (motor pins).
  Wire.begin(PIN_SDA, PIN_SCL, kI2cHz);
  // The BNO085 stretches the clock; give transactions some slack.
  Wire.setTimeOut(kI2cTimeoutMs);
}

bool i2cProbe(uint8_t address) {
  Wire.beginTransmission(address);
  return Wire.endTransmission() == 0;
}

bool i2cWriteReg8(uint8_t address, uint8_t reg, uint8_t value) {
  Wire.beginTransmission(address);
  Wire.write(reg);
  Wire.write(value);
  if (Wire.endTransmission() != 0) {
    g_errors++;
    return false;
  }
  return true;
}

bool i2cReadRegs(uint8_t address, uint8_t reg, uint8_t* out, size_t len) {
  Wire.beginTransmission(address);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    g_errors++;
    return false;
  }
  size_t got = Wire.requestFrom(static_cast<uint16_t>(address), len, true);
  if (got != len) {
    g_errors++;
    while (Wire.available()) {
      Wire.read();
    }
    return false;
  }
  for (size_t i = 0; i < len; i++) {
    out[i] = static_cast<uint8_t>(Wire.read());
  }
  return true;
}

size_t i2cScan(uint8_t* found, size_t maxFound) {
  size_t n = 0;
  for (uint8_t addr = 0x08; addr < 0x78 && n < maxFound; addr++) {
    if (i2cProbe(addr)) {
      found[n++] = addr;
    }
  }
  return n;
}

uint32_t i2cErrorCount() {
  return g_errors;
}
