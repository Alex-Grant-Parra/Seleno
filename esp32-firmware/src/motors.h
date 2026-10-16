// Named motors on the two fixed axes. Axis 0 is RA (U7, J1), axis 1 is DEC
// (U8, J2). They boot registered as "motor1" and "motor2", disengaged.
#pragma once

#include <ArduinoJson.h>
#include <cstdint>

#include "stepper.h"

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

bool motorSetSpeedSps(int axis, double stepsPerSecond, const char** error);
bool motorSetSpeedUs(int axis, uint32_t stepDelayUs, const char** error);
double motorSpeedSps(int axis);

bool motorTurnDegrees(int axis, float degrees, bool forward, const char** error);
bool motorMoveSteps(int axis, int32_t steps, const char** error);
bool motorStartContinuous(int axis, bool forward, const char** error);
void motorStop(int axis);
void motorsStopAll(MotionEnd reason);

uint32_t motorStepsPerRev(int axis);
uint16_t motorMicrosteps(int axis);
// Refused while moving. Rescales the position counter and speed so the
// shaft angle and angular speed are unchanged.
bool motorSetMicrosteps(int axis, uint16_t microsteps, const char** error);

void motorStatusJson(int axis, JsonObject out);

// Emits motion_done events. Call from loop().
void motorsPoll();
