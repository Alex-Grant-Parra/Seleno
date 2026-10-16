// Pure step/microstep arithmetic, free of Arduino headers so it can be unit
// tested on the host (test/test_logic).
#pragma once

#include <cmath>
#include <cstdint>

namespace seleno {

// The TMC2209 accepts 1, 2, 4 ... 256 microsteps per full step.
inline bool isValidMicrosteps(uint32_t microsteps) {
  return microsteps >= 1 && microsteps <= 256 && (microsteps & (microsteps - 1)) == 0;
}

// CHOPCONF.MRES: 0 = 256 microsteps ... 8 = full step.
inline uint8_t mresFromMicrosteps(uint32_t microsteps) {
  uint8_t mres = 8;
  while (microsteps > 1) {
    microsteps >>= 1;
    mres--;
  }
  return mres;
}

inline uint32_t microstepsFromMres(uint8_t mres) {
  return mres > 8 ? 1u : (256u >> mres);
}

// Phase-accumulator increment for a step rate. Every tick adds this to a
// 32-bit accumulator and a carry out is one step, so the average rate is
// exact to 2^-32 of the tick rate: a sidereal rate keeps its accuracy over
// days, which a per-step microsecond delay can't.
inline uint32_t phaseIncrementForRate(double stepsPerSecond, double tickHz) {
  if (!(stepsPerSecond > 0.0) || !(tickHz > 0.0)) {
    return 0;
  }
  double inc = std::floor(stepsPerSecond / tickHz * 4294967296.0 + 0.5);
  // Keep at most one step every other tick, so a STEP pulse always has a
  // low tick between highs.
  const double maxInc = 2147483647.0;
  if (inc > maxInc) {
    inc = maxInc;
  }
  if (inc < 1.0) {
    inc = 1.0;
  }
  return static_cast<uint32_t>(inc);
}

inline double rateForPhaseIncrement(uint32_t inc, double tickHz) {
  return static_cast<double>(inc) * tickHz / 4294967296.0;
}

// TPWMTHRS is compared with TSTEP: the time between two 1/256 microsteps in
// units of 1/fCLK (12 MHz internal clock). StealthChop stays on while
// TSTEP >= TPWMTHRS, i.e. below the threshold speed.
inline uint32_t tpwmthrsForRate(double stepsPerSecond, uint32_t microsteps, double fclkHz = 12000000.0) {
  if (!(stepsPerSecond > 0.0) || microsteps == 0) {
    return 0;  // 0 disables the switch: StealthChop at every speed
  }
  double microstep256PerSecond = stepsPerSecond * (256.0 / microsteps);
  double tstep = fclkHz / microstep256PerSecond;
  if (tstep > 1048575.0) {
    tstep = 1048575.0;  // 20-bit register
  }
  return static_cast<uint32_t>(tstep);
}

// A microstep change keeps the shaft angle, so step counts scale with it.
inline int32_t rescaleSteps(int32_t steps, uint32_t oldMicrosteps, uint32_t newMicrosteps) {
  if (oldMicrosteps == 0 || newMicrosteps == oldMicrosteps) {
    return steps;
  }
  double scaled = static_cast<double>(steps) * newMicrosteps / oldMicrosteps;
  return static_cast<int32_t>(std::llround(scaled));
}

// IRUN/IHOLD current scale for a target RMS current, as TMCStepper computes
// it for vsense = 0 (report: I_RMS = (CS+1) x 42.2 mA with 0.15 ohm).
inline uint8_t currentScaleForMa(double rmsMa, double rsenseOhm = 0.15, double vfs = 0.325) {
  double cs = 32.0 * 1.41421356 * (rmsMa / 1000.0) * (rsenseOhm + 0.02) / vfs - 1.0;
  if (cs < 0.0) cs = 0.0;
  if (cs > 31.0) cs = 31.0;
  return static_cast<uint8_t>(cs);
}

}  // namespace seleno
