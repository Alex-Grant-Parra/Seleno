// Debounce for up to 16 active-low buttons sampled as one bit mask.
#pragma once

#include <cstdint>

namespace seleno {

class MaskDebouncer {
 public:
  explicit MaskDebouncer(uint16_t watchedMask, uint32_t debounceMs)
      : watched_(watchedMask), debounceMs_(debounceMs) {}

  // Seed with a raw reading so the first update reports no change.
  void reset(uint16_t rawPressedMask, uint32_t nowMs) {
    stable_ = rawPressedMask & watched_;
    candidate_ = stable_;
    candidateSinceMs_ = nowMs;
  }

  // rawPressedMask has a 1 for every button currently held down. Returns the
  // bits whose debounced state changed in this call.
  uint16_t update(uint16_t rawPressedMask, uint32_t nowMs) {
    uint16_t raw = rawPressedMask & watched_;
    if (raw != candidate_) {
      candidate_ = raw;
      candidateSinceMs_ = nowMs;
    }
    if (candidate_ == stable_ || (nowMs - candidateSinceMs_) < debounceMs_) {
      return 0;
    }
    uint16_t changed = candidate_ ^ stable_;
    stable_ = candidate_;
    return changed;
  }

  // True while a reading differs from the debounced state, so the caller
  // keeps sampling until it settles.
  bool pending() const { return candidate_ != stable_; }
  uint16_t stable() const { return stable_; }

 private:
  uint16_t watched_;
  uint32_t debounceMs_;
  uint16_t stable_ = 0;
  uint16_t candidate_ = 0;
  uint32_t candidateSinceMs_ = 0;
};

}  // namespace seleno
