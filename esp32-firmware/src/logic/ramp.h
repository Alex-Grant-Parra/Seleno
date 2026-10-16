// Trapezoidal speed ramp for one axis, advanced in fixed time steps.
// Pure arithmetic so it can be unit tested on the host.
#pragma once

#include <cstdint>

namespace seleno {

struct RampInput {
  double rate;         // current pulse rate, pulses/s
  double target;       // cruise rate, pulses/s
  double startRate;    // rates up to this need no ramp (pull-in speed)
  double accel;        // pulses/s^2; <= 0 disables ramping
  int32_t remaining;   // pulses left in a finite move; < 0 = continuous
  bool stopping;       // ramp down, then end the move
  double dt;           // seconds since the last step
};

struct RampOutput {
  double rate;
  bool stopNow;  // stopping and slow enough to end the move here
};

// Speeds up toward the target, slows down early enough that a finite move
// is at the start rate by its last pulse, and never drops below
// min(startRate, target) while moving, so a move always finishes on its
// exact pulse count.
inline RampOutput rampStep(const RampInput& in) {
  RampOutput out{in.rate, false};
  if (in.accel <= 0.0) {
    out.rate = in.target;
    out.stopNow = in.stopping;
    return out;
  }
  double floorRate = in.startRate < in.target ? in.startRate : in.target;
  double desired = in.stopping ? 0.0 : in.target;
  if (!in.stopping && in.remaining >= 0) {
    double braking = (in.rate * in.rate - floorRate * floorRate) / (2.0 * in.accel);
    if (static_cast<double>(in.remaining) <= braking + 1.0) {
      desired = floorRate;
    }
  }
  double dv = in.accel * in.dt;
  double v = in.rate;
  if (v < desired) {
    v = v + dv > desired ? desired : v + dv;
  } else if (v > desired) {
    v = v - dv < desired ? desired : v - dv;
  }
  if (v < floorRate) {
    v = floorRate;
  }
  out.rate = v;
  out.stopNow = in.stopping && v <= floorRate;
  return out;
}

}  // namespace seleno
