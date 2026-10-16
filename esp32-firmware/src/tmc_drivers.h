// TMC2209 drivers U7 (axis 0, address 0) and U8 (axis 1, address 1) on one
// single-wire UART (report: "Stepper drivers: U7 and U8").
//
// Only the main loop task talks on the TMC UART.
#pragma once

#include <ArduinoJson.h>
#include <cstdint>

enum class ChopperMode : uint8_t {
  StealthChop,  // quiet voltage-mode chopper ("silent")
  SpreadCycle,  // classic current-mode chopper, more torque at speed
  Hybrid,       // StealthChop below hybridThresholdSps, SpreadCycle above
};

const char* chopperModeName(ChopperMode mode);
bool parseChopperMode(const char* text, ChopperMode& out);

struct DriverSettings {
  uint16_t microsteps;
  uint16_t runCurrentMa;
  float holdMultiplier;      // hold current = run current x this
  ChopperMode chopper;
  double hybridThresholdSps; // in full-resolution steps/s at `microsteps`
  bool interpolate;          // 256-microstep interpolation (intpol)
  uint8_t stallThreshold;    // SGTHRS; 0 disables StallGuard on DIAG
  bool invertDirection;      // GCONF.shaft
};

// Sets ENN high on both drivers, opens the UART, probes and configures them.
void driversBegin();

bool driverPresent(int axis);
const DriverSettings& driverSettings(int axis);

// Each setter updates the cached settings and, if the driver answers, writes
// them. Return false with a reason in *error on failure.
bool driverSetMicrosteps(int axis, uint16_t microsteps, const char** error);
bool driverSetChopper(int axis, ChopperMode mode, double hybridThresholdSps, const char** error);
bool driverSetCurrent(int axis, uint16_t runCurrentMa, float holdMultiplier, const char** error);
bool driverSetInterpolation(int axis, bool on, const char** error);
bool driverSetStallThreshold(int axis, uint8_t threshold, const char** error);
bool driverSetInvertDirection(int axis, bool invert, const char** error);

// ENN control. On the real board enabling fails unless the driver answered
// on UART and took its configuration, so a driver is never energised with
// strap defaults (U7 1/8, U8 1/32).
bool driverSetEnabled(int axis, bool on, const char** error);
bool driverEnabled(int axis);

// Re-probe and re-apply settings (e.g. after fitting a driver or a reset).
bool driverReinit(int axis);

// Fill a JSON object with the live driver status (reads registers).
void driverStatusJson(int axis, JsonObject out);
// Raw register access for bring-up. Returns false if the read failed.
bool driverReadRegister(int axis, uint8_t reg, uint32_t& value);
bool driverWriteRegister(int axis, uint8_t reg, uint32_t value);

// Main-loop housekeeping: DIAG edges, GSTAT reset detection, over-temperature
// warnings and re-probing missing drivers. Emits events.
void driversPoll();
