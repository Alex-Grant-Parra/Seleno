// Step pulse generation for both axes from one hardware-timer interrupt.
//
// Each tick (kStepTickHz) the ISR adds an axis's phase increment to a 32-bit
// accumulator; a carry out raises STEP, and the next tick lowers it. The
// long-run rate is exact, so sidereal tracking doesn't drift, and nothing
// busy-waits.
//
// Position is kept in 1/256 microsteps. Each STEP pulse moves it by the
// axis's pulse size (256 / the driver's microstep setting), so the count
// stays exact when the microstep setting changes between moves.
#pragma once

#include <cstdint>

enum class MotionEnd : uint8_t {
  None = 0,
  Complete,   // a finite move reached its pulse count
  Stopped,    // stop command
  EmergencyStop,
  DriverFault,
  Disengaged,  // driver switched off mid-move (immediate stop)
};

const char* motionEndName(MotionEnd reason);

constexpr int kAxisCount = 2;

void stepperBegin(const int stepPins[kAxisCount], const int dirPins[kAxisCount]);

// How far one STEP pulse moves, in 1/256 microsteps (256 / microsteps).
// Only changes while the axis is idle; returns false while it moves.
bool stepperSetPulseSize(int axis, uint16_t units256);
uint16_t stepperPulseSize(int axis);

// Start a move of `pulses` STEP pulses; pulses < 0 runs until stopped.
// forward sets DIR high. Returns false if the axis is already moving.
bool stepperStart(int axis, int32_t pulses, bool forward, double pulsesPerSecond);
// Change the cruise pulse rate of a running or future move. With a ramp
// set, a running move accelerates or decelerates to it.
void stepperSetRate(int axis, double pulsesPerSecond);
// The cruise (target) rate, and the instantaneous rate (0 when idle).
double stepperRate(int axis);
double stepperCurrentRate(int axis);
// Acceleration in pulses/s^2 (0 = none) and the rate a move may start at
// without ramping. Moves accelerate from startRate to the cruise rate and
// decelerate so they reach startRate by their last pulse.
void stepperSetRamp(int axis, double accel, double startRate);
// End the move; safe from any task. Stopped ramps down first when moving
// faster than the start rate; EmergencyStop and DriverFault are immediate.
void stepperRequestStop(int axis, MotionEnd reason);
void stepperRequestStopAll(MotionEnd reason);

bool stepperIsMoving(int axis);
// Position in 1/256 microsteps.
int64_t stepperPosition256(int axis);
void stepperSetPosition256(int axis, int64_t position);
// Pulses still to go on a finite move; -1 for continuous; 0 when idle.
int32_t stepperPulsesRemaining(int axis);

// Returns the reason the last move ended, once, then None. The main loop
// polls this to emit motion_done events.
MotionEnd stepperTakeMotionEnd(int axis);
