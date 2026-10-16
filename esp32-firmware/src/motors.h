// Named motors on the two fixed axes. Axis 0 is RA (U7, J1), axis 1 is DEC
// (U8, J2). They boot registered as "motor1" and "motor2", disengaged.
//
// Positions and speeds are in logical steps: steps_per_rev per revolution
// (default 1600, i.e. 1/8 steps). That unit is independent of the driver's
// microstep setting, which by default is chosen automatically when each move
// starts: fine for slow tracking, coarse for fast slews.
#pragma once

#include <ArduinoJson.h>
#include <cstdint>

#include "stepper.h"

struct AutoMicrostepParams {
  uint16_t minMicrosteps;
  uint16_t maxMicrosteps;
  float maxPulseHz;
};

void motorsBegin();

// -1 if no motor has that id.
int motorAxisById(const char* id);
// nullptr if the axis has no motor registered.
const char* motorIdForAxis(int axis);
// The axis whose STEP/DIR/EN pins these are, or -1.
int motorAxisForPins(int step, int dir, int en);
void motorAxisPins(int axis, int& step, int& dir, int& en);

bool motorRegister(int axis, const char* id, const char** error);
// Stops, disengages and frees the axis.
void motorUnregister(int axis);

bool motorEngage(int axis, bool on, const char** error);
bool motorEngaged(int axis);

// Speed in logical steps per second.
bool motorSetSpeedSps(int axis, double stepsPerSecond, const char** error);
bool motorSetSpeedUs(int axis, uint32_t stepDelayUs, const char** error);
double motorSpeedSps(int axis);

bool motorTurnDegrees(int axis, float degrees, bool forward, const char** error);
bool motorMoveSteps(int axis, int32_t steps, const char** error);
bool motorStartContinuous(int axis, bool forward, const char** error);
void motorStop(int axis);
void motorsStopAll(MotionEnd reason);

// Logical resolution: steps_per_rev = 200 x logicalMicrosteps.
uint32_t motorStepsPerRev(int axis);
uint16_t motorLogicalMicrosteps(int axis);
// Refused while moving. The position keeps its angle; the stored speed keeps
// its angular speed.
bool motorSetResolution(int axis, uint16_t logicalMicrosteps, const char** error);

// Logical position (floor of the exact 1/256-microstep count).
int64_t motorPosition(int axis);
int64_t motorPosition256(int axis);
void motorSetPosition(int axis, int64_t logicalSteps);

// The driver's current microstep setting.
uint16_t motorMicrosteps(int axis);
bool motorMicrostepsAuto(int axis);
// microsteps = 0 selects automatic; otherwise a fixed power of two, applied
// now (refused while moving).
bool motorSetMicrosteps(int axis, uint16_t microsteps, const char** error);
const AutoMicrostepParams& motorAutoParams(int axis);
bool motorSetAutoParams(int axis, const AutoMicrostepParams& params, const char** error);

// Acceleration ramp in logical steps/s^2 (0 = start and stop instantly) and
// the speed a move may start at without ramping.
bool motorSetAccel(int axis, double accelSps2, double startSps, const char** error);
double motorAccel(int axis);
double motorStartSpeed(int axis);

void motorStatusJson(int axis, JsonObject out);

// Who started a move. The front-panel UI's moves are Panel moves; host
// commands start Host moves. Only the main loop task starts moves.
enum class MotionSource : uint8_t { Host, Panel };
const char* motionSourceName(MotionSource source);
MotionSource motorMotionSource(int axis);

// Moves started while one of these is alive count as Panel moves.
class PanelMotionScope {
 public:
  PanelMotionScope();
  ~PanelMotionScope();
  PanelMotionScope(const PanelMotionScope&) = delete;
  PanelMotionScope& operator=(const PanelMotionScope&) = delete;

 private:
  MotionSource previous_;
};

// The fastest Host move running now, in full steps per second (cruise or
// instantaneous, whichever is higher); 0 when no Host move is running.
// *anyMoving says whether any Host move runs at all. Safe from any task.
double motorsHostMotionFsps(bool* anyMoving);

// While held, new Host moves are refused (Panel moves still run). Set by
// the front panel's double-RETURN stop until the host acknowledges it.
void motorsSetHostHold(bool held);
bool motorsHostHold();

// Emits motion_done events. Call from loop().
void motorsPoll();
