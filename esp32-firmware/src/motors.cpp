#include "motors.h"

#include <Arduino.h>
#include <cmath>
#include <cstring>

#include "board_pins.h"
#include "config.h"
#include "estop.h"
#include "host_link.h"
#include "logic/step_math.h"
#include "tmc_drivers.h"

namespace {

struct MotorSlot {
  char id[24];
  bool registered;
};

const int kStepPins[kAxisCount] = {PIN_M1_STEP, PIN_M2_STEP};
const int kDirPins[kAxisCount] = {PIN_M1_DIR, PIN_M2_DIR};
const int kEnPins[kAxisCount] = {PIN_M1_EN, PIN_M2_EN};
const char* const kDefaultIds[kAxisCount] = {"motor1", "motor2"};

MotorSlot g_slots[kAxisCount];

bool validAxis(int axis) {
  return axis >= 0 && axis < kAxisCount;
}

bool checkCanMove(int axis, const char** error) {
  if (!validAxis(axis) || !g_slots[axis].registered) {
    if (error) *error = "Unknown motor id";
    return false;
  }
  if (estopLatched()) {
    if (error) *error = "Emergency stop active";
    return false;
  }
  if (!driverEnabled(axis)) {
    if (error) *error = "Motor not engaged";
    return false;
  }
  if (stepperIsMoving(axis)) {
    if (error) *error = "Motor is already moving";
    return false;
  }
  return true;
}

}  // namespace

void motorsBegin() {
  stepperBegin(kStepPins, kDirPins);
  for (int i = 0; i < kAxisCount; i++) {
    strncpy(g_slots[i].id, kDefaultIds[i], sizeof(g_slots[i].id) - 1);
    g_slots[i].id[sizeof(g_slots[i].id) - 1] = '\0';
    g_slots[i].registered = true;
  }
}

int motorAxisById(const char* id) {
  if (id == nullptr || id[0] == '\0') {
    return -1;
  }
  for (int i = 0; i < kAxisCount; i++) {
    if (g_slots[i].registered && strcmp(g_slots[i].id, id) == 0) {
      return i;
    }
  }
  return -1;
}

const char* motorIdForAxis(int axis) {
  if (!validAxis(axis) || !g_slots[axis].registered) {
    return nullptr;
  }
  return g_slots[axis].id;
}

int motorAxisForPins(int step, int dir, int en) {
  for (int i = 0; i < kAxisCount; i++) {
    if (kStepPins[i] == step && kDirPins[i] == dir && kEnPins[i] == en) {
      return i;
    }
  }
  return -1;
}

void motorAxisPins(int axis, int& step, int& dir, int& en) {
  if (!validAxis(axis)) {
    step = dir = en = -1;
    return;
  }
  step = kStepPins[axis];
  dir = kDirPins[axis];
  en = kEnPins[axis];
}

bool motorRegister(int axis, const char* id, const char** error) {
  if (!validAxis(axis)) {
    if (error) *error = "Invalid axis";
    return false;
  }
  if (id == nullptr || id[0] == '\0' || strlen(id) >= sizeof(g_slots[axis].id)) {
    if (error) *error = "Motor id must be 1-23 characters";
    return false;
  }
  int existing = motorAxisById(id);
  if (existing >= 0 && existing != axis) {
    if (error) *error = "Motor id already used by the other axis";
    return false;
  }
  if (g_slots[axis].registered && strcmp(g_slots[axis].id, id) != 0) {
    if (error) *error = "Axis already registered under another id";
    return false;
  }
  strncpy(g_slots[axis].id, id, sizeof(g_slots[axis].id) - 1);
  g_slots[axis].id[sizeof(g_slots[axis].id) - 1] = '\0';
  g_slots[axis].registered = true;
  return true;
}

void motorUnregister(int axis) {
  if (!validAxis(axis)) {
    return;
  }
  stepperRequestStop(axis, MotionEnd::Stopped);
  driverSetEnabled(axis, false, nullptr);
  g_slots[axis].registered = false;
  g_slots[axis].id[0] = '\0';
}

bool motorEngage(int axis, bool on, const char** error) {
  if (!validAxis(axis) || !g_slots[axis].registered) {
    if (error) *error = "Unknown motor id";
    return false;
  }
  if (!on) {
    // A driver with ENN high doesn't move, so further steps would only
    // corrupt the position count.
    stepperRequestStop(axis, MotionEnd::Stopped);
  }
  return driverSetEnabled(axis, on, error);
}

bool motorEngaged(int axis) {
  return driverEnabled(axis);
}

bool motorSetSpeedSps(int axis, double stepsPerSecond, const char** error) {
  if (!validAxis(axis)) {
    if (error) *error = "Unknown motor id";
    return false;
  }
  if (!(stepsPerSecond > 0.0) || !std::isfinite(stepsPerSecond)) {
    if (error) *error = "Invalid sps";
    return false;
  }
  stepperSetRate(axis, stepsPerSecond);
  return true;
}

bool motorSetSpeedUs(int axis, uint32_t stepDelayUs, const char** error) {
  if (stepDelayUs == 0) {
    if (error) *error = "Invalid speed_us";
    return false;
  }
  return motorSetSpeedSps(axis, 1000000.0 / stepDelayUs, error);
}

double motorSpeedSps(int axis) {
  return stepperRate(axis);
}

bool motorTurnDegrees(int axis, float degrees, bool forward, const char** error) {
  if (!std::isfinite(degrees)) {
    if (error) *error = "Invalid degrees";
    return false;
  }
  // Negative angles turn the other way.
  if (degrees < 0.0f) {
    degrees = -degrees;
    forward = !forward;
  }
  double steps = static_cast<double>(degrees) / 360.0 * motorStepsPerRev(axis);
  if (steps >= 2147483647.0) {
    if (error) *error = "Move too large";
    return false;
  }
  int32_t whole = static_cast<int32_t>(steps);
  if (whole == 0) {
    // Smaller than one step: nothing to do, same as the old firmware.
    return checkCanMove(axis, error);
  }
  return motorMoveSteps(axis, forward ? whole : -whole, error);
}

bool motorMoveSteps(int axis, int32_t steps, const char** error) {
  if (!checkCanMove(axis, error)) {
    return false;
  }
  if (steps == 0) {
    return true;
  }
  bool forward = steps > 0;
  int32_t count = forward ? steps : -steps;
  if (!stepperStart(axis, count, forward, 0.0)) {
    if (error) *error = "Motor is already moving";
    return false;
  }
  return true;
}

bool motorStartContinuous(int axis, bool forward, const char** error) {
  if (!checkCanMove(axis, error)) {
    return false;
  }
  if (!stepperStart(axis, -1, forward, 0.0)) {
    if (error) *error = "Motor is already moving";
    return false;
  }
  return true;
}

void motorStop(int axis) {
  stepperRequestStop(axis, MotionEnd::Stopped);
}

void motorsStopAll(MotionEnd reason) {
  stepperRequestStopAll(reason);
}

uint16_t motorMicrosteps(int axis) {
  return driverSettings(axis).microsteps;
}

uint32_t motorStepsPerRev(int axis) {
  return static_cast<uint32_t>(kFullStepsPerRev) * motorMicrosteps(axis);
}

bool motorSetMicrosteps(int axis, uint16_t microsteps, const char** error) {
  if (!validAxis(axis)) {
    if (error) *error = "Unknown motor id";
    return false;
  }
  if (stepperIsMoving(axis)) {
    if (error) *error = "Stop the motor before changing microsteps";
    return false;
  }
  uint16_t old = motorMicrosteps(axis);
  if (microsteps == old) {
    return driverSetMicrosteps(axis, microsteps, error);  // re-assert on the chip
  }
  if (!driverSetMicrosteps(axis, microsteps, error)) {
    return false;
  }
  stepperSetPosition(axis, seleno::rescaleSteps(stepperPosition(axis), old, microsteps));
  stepperSetRate(axis, stepperRate(axis) * microsteps / old);
  return true;
}

void motorStatusJson(int axis, JsonObject out) {
  out["motor"] = motorIdForAxis(axis) != nullptr ? motorIdForAxis(axis) : "";
  out["axis"] = axis;
  out["enabled"] = motorEngaged(axis);
  out["moving"] = stepperIsMoving(axis);
  out["position"] = stepperPosition(axis);
  out["steps_remaining"] = stepperStepsRemaining(axis);
  out["speed_sps"] = stepperRate(axis);
  double rate = stepperRate(axis);
  out["speed_us"] = rate > 0.0 ? static_cast<uint32_t>(1000000.0 / rate + 0.5) : 0;
  out["microsteps"] = motorMicrosteps(axis);
  out["steps_per_rev"] = motorStepsPerRev(axis);
  out["driver_present"] = driverPresent(axis);
  out["chopper"] = chopperModeName(driverSettings(axis).chopper);
}

void motorsPoll() {
  for (int i = 0; i < kAxisCount; i++) {
    MotionEnd end = stepperTakeMotionEnd(i);
    if (end == MotionEnd::None) {
      continue;
    }
    JsonDocument ev;
    ev["event"] = "motion_done";
    ev["motor"] = motorIdForAxis(i) != nullptr ? motorIdForAxis(i) : "";
    ev["axis"] = i;
    ev["reason"] = motionEndName(end);
    ev["position"] = stepperPosition(i);
    hostEmitEvent(ev);
  }
}
