// Step pulse generation for both axes from one hardware-timer interrupt.
//
// Each tick (kStepTickHz) the ISR adds an axis's phase increment to a 32-bit
// accumulator; a carry out raises STEP, and the next tick lowers it. The
// long-run rate is exact, so sidereal tracking doesn't drift, and nothing
// busy-waits.
#pragma once

#include <cstdint>

enum class MotionEnd : uint8_t {
  None = 0,
  Complete,   // a finite move reached its step count
  Stopped,    // stop command
  EmergencyStop,
  DriverFault,
};

const char* motionEndName(MotionEnd reason);

constexpr int kAxisCount = 2;

void stepperBegin(const int stepPins[kAxisCount], const int dirPins[kAxisCount]);

// Start a move. steps < 0 runs until stopped. forward sets DIR high.
// Returns false if the axis is already moving.
bool stepperStart(int axis, int32_t steps, bool forward, double stepsPerSecond);
// Change the rate of a running or future move.
void stepperSetRate(int axis, double stepsPerSecond);
double stepperRate(int axis);
// Ask the ISR to end the move at the next tick; safe from any task.
void stepperRequestStop(int axis, MotionEnd reason);
void stepperRequestStopAll(MotionEnd reason);

bool stepperIsMoving(int axis);
int32_t stepperPosition(int axis);
void stepperSetPosition(int axis, int32_t position);
// Steps still to go on a finite move; -1 for continuous; 0 when idle.
int32_t stepperStepsRemaining(int axis);

// Returns the reason the last move ended, once, then None. The main loop
// polls this to emit motion_done events.
MotionEnd stepperTakeMotionEnd(int axis);
