#include "stepper.h"

#include <Arduino.h>
#include <esp_intr_alloc.h>
#include <esp_timer.h>
#include <soc/gpio_struct.h>

#include "config.h"
#include "logic/ramp.h"
#include "logic/step_math.h"

namespace {

struct Axis {
  uint32_t stepMask;  // all step/dir pins on this board are below 32
  uint32_t dirMask;
  uint32_t phase;
  uint32_t phaseInc;
  int64_t position256;     // 1/256 microsteps
  int32_t pulseDelta;      // +/- pulse size for the current direction
  uint16_t pulseSize;      // 1/256 microsteps per STEP pulse
  int32_t pulsesRemaining;  // -1 = continuous
  bool running;
  bool pulseHigh;
  MotionEnd stopRequest;
  MotionEnd lastEnd;
  // Ramp state, used by the 1 kHz ramp task (not the ISR).
  double targetRate;   // pulses/s
  double currentRate;  // pulses/s, what phaseInc is set to
  double accel;        // pulses/s^2, 0 = no ramp
  double startRate;    // pulses/s reached without ramping
  bool stopping;       // ramped stop in progress
};

DRAM_ATTR Axis g_axes[kAxisCount];
portMUX_TYPE g_mux = portMUX_INITIALIZER_UNLOCKED;
hw_timer_t* g_timer = nullptr;
esp_timer_handle_t g_rampTimer = nullptr;
constexpr uint32_t kRampPeriodUs = 1000;

double clampRate(double rate) {
  return rate > kMaxStepRateSps ? kMaxStepRateSps : rate;
}

void IRAM_ATTR finishMove(Axis& a, MotionEnd reason) {
  a.running = false;
  a.pulsesRemaining = 0;
  a.stopRequest = MotionEnd::None;
  a.lastEnd = reason;
}

void IRAM_ATTR onStepTick() {
  portENTER_CRITICAL_ISR(&g_mux);
  for (int i = 0; i < kAxisCount; i++) {
    Axis& a = g_axes[i];
    if (a.pulseHigh) {
      GPIO.out_w1tc = a.stepMask;
      a.pulseHigh = false;
    }
    if (!a.running) {
      continue;
    }
    if (a.stopRequest != MotionEnd::None) {
      finishMove(a, a.stopRequest);
      continue;
    }
    uint32_t before = a.phase;
    a.phase += a.phaseInc;
    if (a.phase < before) {
      GPIO.out_w1ts = a.stepMask;
      a.pulseHigh = true;
      a.position256 += a.pulseDelta;
      if (a.pulsesRemaining > 0 && --a.pulsesRemaining == 0) {
        finishMove(a, MotionEnd::Complete);
      }
    }
  }
  portEXIT_CRITICAL_ISR(&g_mux);
}

bool validAxis(int axis) {
  return axis >= 0 && axis < kAxisCount;
}

// Every millisecond: move each running axis's rate one step along its ramp.
// The ISR only ever sees the resulting phase increment.
void onRampTick(void*) {
  constexpr double dt = kRampPeriodUs / 1000000.0;
  for (int i = 0; i < kAxisCount; i++) {
    Axis& a = g_axes[i];
    seleno::RampInput in;
    portENTER_CRITICAL(&g_mux);
    bool active = a.running && (a.accel > 0.0) && (a.currentRate != a.targetRate || a.stopping ||
                                                    a.pulsesRemaining >= 0);
    in.rate = a.currentRate;
    in.target = a.targetRate;
    in.startRate = a.startRate;
    in.accel = a.accel;
    in.remaining = a.pulsesRemaining;
    in.stopping = a.stopping;
    portEXIT_CRITICAL(&g_mux);
    if (!active) {
      continue;
    }
    in.dt = dt;
    seleno::RampOutput out = seleno::rampStep(in);
    uint32_t inc = seleno::phaseIncrementForRate(out.rate, kStepTickHz);
    portENTER_CRITICAL(&g_mux);
    if (a.running) {
      a.currentRate = out.rate;
      a.phaseInc = inc;
      if (out.stopNow && a.stopping && a.stopRequest == MotionEnd::None) {
        a.stopRequest = MotionEnd::Stopped;
        a.stopping = false;
      }
    }
    portEXIT_CRITICAL(&g_mux);
  }
}

}  // namespace

const char* motionEndName(MotionEnd reason) {
  switch (reason) {
    case MotionEnd::Complete: return "complete";
    case MotionEnd::Stopped: return "stopped";
    case MotionEnd::EmergencyStop: return "estop";
    case MotionEnd::DriverFault: return "driver_fault";
    case MotionEnd::Disengaged: return "disengaged";
    default: return "none";
  }
}

void stepperBegin(const int stepPins[kAxisCount], const int dirPins[kAxisCount]) {
  for (int i = 0; i < kAxisCount; i++) {
    pinMode(stepPins[i], OUTPUT);
    pinMode(dirPins[i], OUTPUT);
    digitalWrite(stepPins[i], LOW);
    digitalWrite(dirPins[i], LOW);
    Axis& a = g_axes[i];
    a = Axis();
    a.stepMask = 1UL << stepPins[i];
    a.dirMask = 1UL << dirPins[i];
    a.pulseSize = 256 / kDefaultMicrosteps;
    a.pulseDelta = a.pulseSize;
    a.targetRate = 1000000.0 / kDefaultStepDelayUs;
    a.currentRate = a.targetRate;
    a.phaseInc = seleno::phaseIncrementForRate(a.targetRate, kStepTickHz);
  }

  esp_timer_create_args_t ramp = {};
  ramp.callback = onRampTick;
  ramp.name = "step_ramp";
  esp_timer_create(&ramp, &g_rampTimer);
  esp_timer_start_periodic(g_rampTimer, kRampPeriodUs);

  // Timer 0 at 1 MHz; the alarm sets the tick. The ISR is placed in IRAM so
  // steps keep coming while LittleFS writes have the flash cache disabled.
  g_timer = timerBegin(0, 80, true);
  timerAttachInterruptFlag(g_timer, onStepTick, false, ESP_INTR_FLAG_IRAM);
  timerAlarmWrite(g_timer, 1000000UL / kStepTickHz, true);
  timerAlarmEnable(g_timer);
}

bool stepperSetPulseSize(int axis, uint16_t units256) {
  if (!validAxis(axis) || units256 == 0 || units256 > 256) {
    return false;
  }
  bool ok = false;
  portENTER_CRITICAL(&g_mux);
  if (!g_axes[axis].running) {
    g_axes[axis].pulseSize = units256;
    ok = true;
  }
  portEXIT_CRITICAL(&g_mux);
  return ok;
}

uint16_t stepperPulseSize(int axis) {
  return validAxis(axis) ? g_axes[axis].pulseSize : 0;
}

bool stepperStart(int axis, int32_t pulses, bool forward, double pulsesPerSecond) {
  if (!validAxis(axis) || pulses == 0) {
    return false;
  }
  Axis& a = g_axes[axis];
  if (a.running) {
    return false;
  }
  // DIR must be stable before the first STEP edge; the first edge comes at
  // the earliest one tick (50 us) after running goes true.
  if (forward) {
    GPIO.out_w1ts = a.dirMask;
  } else {
    GPIO.out_w1tc = a.dirMask;
  }
  portENTER_CRITICAL(&g_mux);
  if (pulsesPerSecond > 0.0) {
    a.targetRate = clampRate(pulsesPerSecond);
  }
  // Start no faster than the pull-in rate; the ramp task takes it from there.
  a.currentRate = (a.accel > 0.0 && a.targetRate > a.startRate) ? a.startRate : a.targetRate;
  uint32_t inc = seleno::phaseIncrementForRate(a.currentRate, kStepTickHz);
  a.stopping = false;
  a.phaseInc = inc;
  a.phase = 0u - inc;  // carry on the next tick: the first step is immediate
  a.pulsesRemaining = pulses < 0 ? -1 : pulses;
  a.pulseDelta = forward ? a.pulseSize : -static_cast<int32_t>(a.pulseSize);
  a.stopRequest = MotionEnd::None;
  a.lastEnd = MotionEnd::None;
  a.running = true;
  portEXIT_CRITICAL(&g_mux);
  return true;
}

void stepperSetRate(int axis, double pulsesPerSecond) {
  if (!validAxis(axis) || !(pulsesPerSecond > 0.0)) {
    return;
  }
  pulsesPerSecond = clampRate(pulsesPerSecond);
  uint32_t inc = seleno::phaseIncrementForRate(pulsesPerSecond, kStepTickHz);
  portENTER_CRITICAL(&g_mux);
  Axis& a = g_axes[axis];
  a.targetRate = pulsesPerSecond;
  // Without a ramp (or while idle) the new rate applies at once; otherwise
  // the ramp task walks currentRate over to it.
  if (!a.running || a.accel <= 0.0) {
    a.currentRate = pulsesPerSecond;
    a.phaseInc = inc;
  }
  portEXIT_CRITICAL(&g_mux);
}

double stepperRate(int axis) {
  return validAxis(axis) ? g_axes[axis].targetRate : 0.0;
}

double stepperCurrentRate(int axis) {
  if (!validAxis(axis)) {
    return 0.0;
  }
  portENTER_CRITICAL(&g_mux);
  double r = g_axes[axis].running ? g_axes[axis].currentRate : 0.0;
  portEXIT_CRITICAL(&g_mux);
  return r;
}

void stepperSetRamp(int axis, double accel, double startRate) {
  if (!validAxis(axis)) {
    return;
  }
  portENTER_CRITICAL(&g_mux);
  g_axes[axis].accel = accel > 0.0 ? accel : 0.0;
  g_axes[axis].startRate = startRate > 0.0 ? clampRate(startRate) : 0.0;
  portEXIT_CRITICAL(&g_mux);
}

void stepperRequestStop(int axis, MotionEnd reason) {
  if (!validAxis(axis)) {
    return;
  }
  portENTER_CRITICAL_SAFE(&g_mux);
  Axis& a = g_axes[axis];
  if (a.running) {
    // A plain stop above the pull-in rate ramps down so the motor doesn't
    // overshoot and lose steps; an emergency stop or fault is immediate.
    if (reason == MotionEnd::Stopped && a.accel > 0.0 && a.currentRate > a.startRate) {
      a.stopping = true;
    } else {
      a.stopRequest = reason;
    }
  }
  portEXIT_CRITICAL_SAFE(&g_mux);
}

void stepperRequestStopAll(MotionEnd reason) {
  for (int i = 0; i < kAxisCount; i++) {
    stepperRequestStop(i, reason);
  }
}

bool stepperIsMoving(int axis) {
  return validAxis(axis) && g_axes[axis].running;
}

int64_t stepperPosition256(int axis) {
  if (!validAxis(axis)) {
    return 0;
  }
  portENTER_CRITICAL(&g_mux);
  int64_t p = g_axes[axis].position256;
  portEXIT_CRITICAL(&g_mux);
  return p;
}

void stepperSetPosition256(int axis, int64_t position) {
  if (!validAxis(axis)) {
    return;
  }
  portENTER_CRITICAL(&g_mux);
  g_axes[axis].position256 = position;
  portEXIT_CRITICAL(&g_mux);
}

int32_t stepperPulsesRemaining(int axis) {
  if (!validAxis(axis)) {
    return 0;
  }
  portENTER_CRITICAL(&g_mux);
  int32_t r = g_axes[axis].running ? g_axes[axis].pulsesRemaining : 0;
  portEXIT_CRITICAL(&g_mux);
  return r;
}

MotionEnd stepperTakeMotionEnd(int axis) {
  if (!validAxis(axis)) {
    return MotionEnd::None;
  }
  portENTER_CRITICAL(&g_mux);
  MotionEnd r = g_axes[axis].lastEnd;
  g_axes[axis].lastEnd = MotionEnd::None;
  portEXIT_CRITICAL(&g_mux);
  return r;
}
