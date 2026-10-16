// The board's single I2C bus (SDA IO33, SCL IO32): MCP23017 at 0x20 and the
// IMU board on J3. The Arduino Wire driver locks each transaction, so the IO
// task, the IMU task and the main loop can share it.
#pragma once

#include <cstddef>
#include <cstdint>

void i2cBegin();
bool i2cProbe(uint8_t address);
bool i2cWriteReg8(uint8_t address, uint8_t reg, uint8_t value);
// Reads len bytes starting at reg (register address auto-increments).
bool i2cReadRegs(uint8_t address, uint8_t reg, uint8_t* out, size_t len);
// Fills found[] with responding addresses; returns how many.
size_t i2cScan(uint8_t* found, size_t maxFound);
uint32_t i2cErrorCount();
