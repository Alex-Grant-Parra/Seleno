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
  uint16_t logicalMicrosteps;  // position/speed unit: 1/logicalMicrosteps steps
  double speedSps;             // logical steps per second
  bool autoMicrosteps;
  AutoMicrostepParams autoParams;
  double accelSps2;  // logical steps/s^2, 0 = no ramp
  double startSps;   // logical steps/s reached without ramping
};

const int kStepPins[kAxisCount] = {PIN_M1_STEP, PIN_M2_STEP};
const int kDirPins[kAxisCount] = {PIN_M1_DIR, PIN_M2_DIR};
const int kEnPins[kAxisCount] = {PIN_M1_EN, PIN_M2_EN};
const char* const kDefaultIds[kAxisCount] = {"motor1", "motor2"};

MotorSlot g_slots[kAxisCount];

MotionSource g_currentSource = MotionSource::Host;     // main loop only
volatile MotionSource g_axisSource[kAxisCount] = {MotionSource::Host, MotionSource::Host};
volatile bool g_hostHold = false;

bool validAxis(int axis) {
  return axis >= 0 && axis < kAxisCount;
}

// 1/256-microstep units in one logical step.
int64_t unitsPerLogicalStep(int axis) {
  return 256 / g_slots[axis].logicalMicrosteps;
}

double fullStepsPerSecond(int axis, double logicalSps) {
  return logicalSps / g_slots[axis].logicalMicrosteps;
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
  if (g_hostHold && g_currentSource == MotionSource::Host) {
    if (error) *error = "Stopped from the front panel (waiting for ui ack_stop)";
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

// The fastest a move of `distance` logical steps actually gets: with a ramp
// it accelerates for half the distance and decelerates for the other half,
// so a short correction never reaches the cruise speed. distance < 0 means
// continuous (it does reach cruise).
double peakSpeed(const MotorSlot& slot, double distance) {
  if (distance < 0.0 || slot.accelSps2 <= 0.0 || slot.speedSps <= slot.startSps) {
    return slot.speedSps;
  }
  double peak = std::sqrt(slot.startSps * slot.startSps + slot.accelSps2 * distance);
  return peak < slot.speedSps ? peak : slot.speedSps;
}

// Pick the microstep setting for a move (automatic mode) from the fastest
// speed it will reach, apply it to the driver, and set the STEP pulse size,
// rate and ramp. The axis is idle here, so the driver can change resolution
// safely and the 1/256 position count carries straight across.
bool prepareMove(int axis, double distance, const char** error) {
  MotorSlot& slot = g_slots[axis];
  double fsps = fullStepsPerSecond(axis, slot.speedSps);
  double peakFsps = fullStepsPerSecond(axis, peakSpeed(slot, distance));
  uint16_t microsteps = motorMicrosteps(axis);
  if (slot.autoMicrosteps) {
    microsteps = static_cast<uint16_t>(seleno::autoMicrosteps(
        peakFsps, slot.autoParams.minMicrosteps, slot.autoParams.maxMicrosteps, slot.autoParams.maxPulseHz));
  }
  double pulseRate = fsps * microsteps;
  // A short move that never reaches cruise only has to fit its peak rate;
  // capping the cruise rate there changes nothing about its motion.
  if (pulseRate > kMaxStepRateSps && peakFsps * microsteps <= kMaxStepRateSps) {
    pulseRate = kMaxStepRateSps;
  }
  if (pulseRate > kMaxStepRateSps) {
    if (error) {
      *error = slot.autoMicrosteps ? "Speed too high even at the coarsest auto microsteps"
                                   : "Speed too high for the fixed microstep setting";
    }
    return false;
  }
  if (microsteps != motorMicrosteps(axis) && !driverApplyMicrosteps(axis, microsteps, error)) {
    return false;
  }
  if (!stepperSetPulseSize(axis, static_cast<uint16_t>(256 / microsteps))) {
    if (error) *error = "Motor is already moving";
    return false;
  }
  // Ramp limits scale with the pulse size just like the speed does.
  double pulsesPerLogicalStep = static_cast<double>(microsteps) / slot.logicalMicrosteps;
  stepperSetRamp(axis, slot.accelSps2 * pulsesPerLogicalStep, slot.startSps * pulsesPerLogicalStep);
  stepperSetRate(axis, pulseRate);
  return true;
}

// Pulses for a move of delta256 (1/256 microsteps) at the current pulse size.
bool pulsesFor(int axis, double delta256, int32_t& pulses, const char** error) {
  double p = std::nearbyint(std::fabs(delta256) / stepperPulseSize(axis));
  if (p >= 2147483647.0) {
    if (error) *error = "Move too large";
    return false;
  }
  pulses = static_cast<int32_t>(p);
  return true;
}

bool startMove(int axis, double delta256, const char** error) {
  double distance = std::fabs(delta256) / unitsPerLogicalStep(axis);
  if (!checkCanMove(axis, error) || !prepareMove(axis, distance, error)) {
    return false;
  }
  int32_t pulses = 0;
  if (!pulsesFor(axis, delta256, pulses, error)) {
    return false;
  }
  if (pulses == 0) {
    return true;  // less than one pulse: nothing to do
  }
  // Tag before starting, so the panel task never sees the move untagged.
  g_axisSource[axis] = g_currentSource;
  if (!stepperStart(axis, pulses, delta256 > 0, 0.0)) {
    if (error) *error = "Motor is already moving";
    return false;
  }
  return true;
}

}  // namespace

void motorsBegin() {
  stepperBegin(kStepPins, kDirPins);
  for (int i = 0; i < kAxisCount; i++) {
    MotorSlot& slot = g_slots[i];
    strncpy(slot.id, kDefaultIds[i], sizeof(slot.id) - 1);
    slot.id[sizeof(slot.id) - 1] = '\0';
    slot.registered = true;
    slot.logicalMicrosteps = kDefaultLogicalMicrosteps;
    slot.speedSps = 1000000.0 / kDefaultStepDelayUs;
    slot.autoMicrosteps = true;
    slot.autoParams = {kAutoMinMicrosteps, kAutoMaxMicrosteps, kAutoMaxPulseHz};
    slot.accelSps2 = kDefaultAccelSps2;
    slot.startSps = kDefaultStartSps;
    stepperSetPulseSize(i, static_cast<uint16_t>(256 / motorMicrosteps(i)));
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
  stepperRequestStop(axis, MotionEnd::Disengaged);
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
    // corrupt the position count: stop at once, no ramp.
    stepperRequestStop(axis, MotionEnd::Disengaged);
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
  if (stepperIsMoving(axis)) {
    // The microstep setting is fixed for the rest of this move.
    double pulseRate = fullStepsPerSecond(axis, stepsPerSecond) * motorMicrosteps(axis);
    if (pulseRate > kMaxStepRateSps) {
      if (error) *error = "Too fast for this move's microstep setting; stop and restart the move";
      return false;
    }
    stepperSetRate(axis, pulseRate);
  }
  g_slots[axis].speedSps = stepsPerSecond;
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
  return validAxis(axis) ? g_slots[axis].speedSps : 0.0;
}

bool motorTurnDegrees(int axis, float degrees, bool forward, const char** error) {
  if (!std::isfinite(degrees)) {
    if (error) *error = "Invalid degrees";
    return false;
  }
  if (!validAxis(axis)) {
    if (error) *error = "Unknown motor id";
    return false;
  }
  double delta256 = static_cast<double>(degrees) / 360.0 * kFullStepsPerRev * 256.0;
  return startMove(axis, forward ? delta256 : -delta256, error);
}

bool motorMoveSteps(int axis, int32_t steps, const char** error) {
  if (!validAxis(axis)) {
    if (error) *error = "Unknown motor id";
    return false;
  }
  return startMove(axis, static_cast<double>(steps) * unitsPerLogicalStep(axis), error);
}

bool motorStartContinuous(int axis, bool forward, const char** error) {
  if (!checkCanMove(axis, error) || !prepareMove(axis, -1.0, error)) {
    return false;
  }
  g_axisSource[axis] = g_currentSource;
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

uint32_t motorStepsPerRev(int axis) {
  return validAxis(axis) ? static_cast<uint32_t>(kFullStepsPerRev) * g_slots[axis].logicalMicrosteps
                         : static_cast<uint32_t>(kFullStepsPerRev) * kDefaultLogicalMicrosteps;
}

uint16_t motorLogicalMicrosteps(int axis) {
  return validAxis(axis) ? g_slots[axis].logicalMicrosteps : kDefaultLogicalMicrosteps;
}

bool motorSetResolution(int axis, uint16_t logicalMicrosteps, const char** error) {
  if (!validAxis(axis)) {
    if (error) *error = "Unknown motor id";
    return false;
  }
  if (!seleno::isValidMicrosteps(logicalMicrosteps)) {
    if (error) *error = "steps_per_rev must be 200 x a power of two (200-51200)";
    return false;
  }
  if (stepperIsMoving(axis)) {
    if (error) *error = "Stop the motor before changing steps_per_rev";
    return false;
  }
  MotorSlot& slot = g_slots[axis];
  double scale = static_cast<double>(logicalMicrosteps) / slot.logicalMicrosteps;
  slot.speedSps *= scale;
  slot.accelSps2 *= scale;
  slot.startSps *= scale;
  slot.logicalMicrosteps = logicalMicrosteps;
  return true;
}

int64_t motorPosition(int axis) {
  if (!validAxis(axis)) return 0;
  return seleno::floorDiv(stepperPosition256(axis), unitsPerLogicalStep(axis));
}

int64_t motorPosition256(int axis) {
  return stepperPosition256(axis);
}

void motorSetPosition(int axis, int64_t logicalSteps) {
  if (!validAxis(axis)) return;
  stepperSetPosition256(axis, logicalSteps * unitsPerLogicalStep(axis));
}

uint16_t motorMicrosteps(int axis) {
  return driverSettings(axis).microsteps;
}

bool motorMicrostepsAuto(int axis) {
  return validAxis(axis) && g_slots[axis].autoMicrosteps;
}

bool motorSetMicrosteps(int axis, uint16_t microsteps, const char** error) {
  if (!validAxis(axis)) {
    if (error) *error = "Unknown motor id";
    return false;
  }
  if (microsteps == 0) {
    g_slots[axis].autoMicrosteps = true;  // takes effect at the next move
    return true;
  }
  if (stepperIsMoving(axis)) {
    if (error) *error = "Stop the motor before changing microsteps";
    return false;
  }
  if (!driverApplyMicrosteps(axis, microsteps, error)) {
    return false;
  }
  stepperSetPulseSize(axis, static_cast<uint16_t>(256 / microsteps));
  g_slots[axis].autoMicrosteps = false;
  return true;
}

const AutoMicrostepParams& motorAutoParams(int axis) {
  static AutoMicrostepParams fallback = {kAutoMinMicrosteps, kAutoMaxMicrosteps, kAutoMaxPulseHz};
  return validAxis(axis) ? g_slots[axis].autoParams : fallback;
}

bool motorSetAutoParams(int axis, const AutoMicrostepParams& params, const char** error) {
  if (!validAxis(axis)) {
    if (error) *error = "Unknown motor id";
    return false;
  }
  if (!seleno::isValidMicrosteps(params.minMicrosteps) || !seleno::isValidMicrosteps(params.maxMicrosteps) ||
      params.minMicrosteps > params.maxMicrosteps) {
    if (error) *error = "min/max microsteps must be powers of two, 1-256, min <= max";
    return false;
  }
  if (!(params.maxPulseHz >= 100.0f) || params.maxPulseHz > kMaxStepRateSps) {
    if (error) *error = "max_pulse_hz must be 100-9999";
    return false;
  }
  g_slots[axis].autoParams = params;
  return true;
}

bool motorSetAccel(int axis, double accelSps2, double startSps, const char** error) {
  if (!validAxis(axis)) {
    if (error) *error = "Unknown motor id";
    return false;
  }
  if (!(accelSps2 >= 0.0) || !std::isfinite(accelSps2) || !(startSps > 0.0) || !std::isfinite(startSps)) {
    if (error) *error = "accel must be >= 0 and start_sps > 0";
    return false;
  }
  MotorSlot& slot = g_slots[axis];
  slot.accelSps2 = accelSps2;
  slot.startSps = startSps;
  // A running move picks the new limits up straight away.
  double pulsesPerLogicalStep = static_cast<double>(motorMicrosteps(axis)) / slot.logicalMicrosteps;
  stepperSetRamp(axis, accelSps2 * pulsesPerLogicalStep, startSps * pulsesPerLogicalStep);
  return true;
}

double motorAccel(int axis) {
  return validAxis(axis) ? g_slots[axis].accelSps2 : 0.0;
}

double motorStartSpeed(int axis) {
  return validAxis(axis) ? g_slots[axis].startSps : 0.0;
}

void motorStatusJson(int axis, JsonObject out) {
  const MotorSlot& slot = g_slots[axis];
  const DriverSettings& drv = driverSettings(axis);
  out["motor"] = motorIdForAxis(axis) != nullptr ? motorIdForAxis(axis) : "";
  out["axis"] = axis;
  out["enabled"] = motorEngaged(axis);
  out["moving"] = stepperIsMoving(axis);
  out["position"] = motorPosition(axis);
  out["position_fine"] = motorPosition256(axis);  // 1/256 microsteps
  out["steps_per_rev"] = motorStepsPerRev(axis);
  out["speed_sps"] = slot.speedSps;
  out["speed_us"] = slot.speedSps > 0.0 ? static_cast<uint32_t>(1000000.0 / slot.speedSps + 0.5) : 0;
  out["accel_sps2"] = slot.accelSps2;
  out["start_sps"] = slot.startSps;
  out["pulses_remaining"] = stepperPulsesRemaining(axis);
  out["pulse_rate"] = stepperRate(axis);
  out["pulse_rate_now"] = stepperCurrentRate(axis);
  out["microsteps"] = drv.microsteps;
  out["microstep_mode"] = slot.autoMicrosteps ? "auto" : "fixed";
  JsonObject autoParams = out["auto"].to<JsonObject>();
  autoParams["min_microsteps"] = slot.autoParams.minMicrosteps;
  autoParams["max_microsteps"] = slot.autoParams.maxMicrosteps;
  autoParams["max_pulse_hz"] = slot.autoParams.maxPulseHz;
  out["chopper"] = chopperModeName(drv.chopper);
  out["chopper_threshold_fsps"] = drv.chopperThresholdFsps;
  out["driver_present"] = driverPresent(axis);
  out["source"] = motionSourceName(g_axisSource[axis]);
}

const char* motionSourceName(MotionSource source) {
  return source == MotionSource::Panel ? "panel" : "host";
}

MotionSource motorMotionSource(int axis) {
  return validAxis(axis) ? g_axisSource[axis] : MotionSource::Host;
}

PanelMotionScope::PanelMotionScope() : previous_(g_currentSource) {
  g_currentSource = MotionSource::Panel;
}

PanelMotionScope::~PanelMotionScope() {
  g_currentSource = previous_;
}

double motorsHostMotionFsps(bool* anyMoving) {
  double fastest = 0.0;
  bool moving = false;
  for (int i = 0; i < kAxisCount; i++) {
    if (g_axisSource[i] != MotionSource::Host) {
      continue;
    }
    double pulsesPerSecond = stepperPeakRate(i);
    if (pulsesPerSecond <= 0.0) {
      continue;
    }
    moving = true;
    double fsps = pulsesPerSecond * stepperPulseSize(i) / 256.0;
    if (fsps > fastest) {
      fastest = fsps;
    }
  }
  if (anyMoving != nullptr) {
    *anyMoving = moving;
  }
  return fastest;
}

void motorsSetHostHold(bool held) {
  g_hostHold = held;
}

bool motorsHostHold() {
  return g_hostHold;
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
    ev["source"] = motionSourceName(g_axisSource[i]);
    ev["position"] = motorPosition(i);
    ev["position_fine"] = motorPosition256(i);
    hostEmitEvent(ev);
  }
}
