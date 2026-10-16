#include "stepper.h"

#include <Arduino.h>
#include <esp_intr_alloc.h>
#include <soc/gpio_struct.h>

#include "config.h"
#include "logic/step_math.h"

namespace {

struct Axis {
  uint32_t stepMask;  // all step/dir pins on this board are below 32
  uint32_t dirMask;
  uint32_t phase;
  uint32_t phaseInc;
  int32_t position;
  int32_t stepsRemaining;  // -1 = continuous
  int8_t dirSign;
  bool running;
  bool pulseHigh;
  MotionEnd stopRequest;
  MotionEnd lastEnd;
  double rateSps;
};

DRAM_ATTR Axis g_axes[kAxisCount];
portMUX_TYPE g_mux = portMUX_INITIALIZER_UNLOCKED;
hw_timer_t* g_timer = nullptr;

void IRAM_ATTR finishMove(Axis& a, MotionEnd reason) {
  a.running = false;
  a.stepsRemaining = 0;
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
      a.position += a.dirSign;
      if (a.stepsRemaining > 0 && --a.stepsRemaining == 0) {
        finishMove(a, MotionEnd::Complete);
      }
    }
  }
  portEXIT_CRITICAL_ISR(&g_mux);
}

bool validAxis(int axis) {
  return axis >= 0 && axis < kAxisCount;
}

}  // namespace

const char* motionEndName(MotionEnd reason) {
  switch (reason) {
    case MotionEnd::Complete: return "complete";
    case MotionEnd::Stopped: return "stopped";
    case MotionEnd::EmergencyStop: return "estop";
    case MotionEnd::DriverFault: return "driver_fault";
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
    a.dirSign = 1;
    a.rateSps = 1000000.0 / kDefaultStepDelayUs;
    a.phaseInc = seleno::phaseIncrementForRate(a.rateSps, kStepTickHz);
  }

  // Timer 0 at 1 MHz; the alarm sets the tick. The ISR is placed in IRAM so
  // steps keep coming while LittleFS writes have the flash cache disabled.
  g_timer = timerBegin(0, 80, true);
  timerAttachInterruptFlag(g_timer, onStepTick, false, ESP_INTR_FLAG_IRAM);
  timerAlarmWrite(g_timer, 1000000UL / kStepTickHz, true);
  timerAlarmEnable(g_timer);
}

bool stepperStart(int axis, int32_t steps, bool forward, double stepsPerSecond) {
  if (!validAxis(axis) || steps == 0) {
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
  if (stepsPerSecond > 0.0) {
    a.rateSps = stepsPerSecond > kMaxStepRateSps ? kMaxStepRateSps : stepsPerSecond;
  }
  uint32_t inc = seleno::phaseIncrementForRate(a.rateSps, kStepTickHz);
  portENTER_CRITICAL(&g_mux);
  a.phaseInc = inc;
  a.phase = 0u - inc;  // carry on the next tick: the first step is immediate
  a.stepsRemaining = steps < 0 ? -1 : steps;
  a.dirSign = forward ? 1 : -1;
  a.stopRequest = MotionEnd::None;
  a.lastEnd = MotionEnd::None;
  a.running = true;
  portEXIT_CRITICAL(&g_mux);
  return true;
}

void stepperSetRate(int axis, double stepsPerSecond) {
  if (!validAxis(axis) || !(stepsPerSecond > 0.0)) {
    return;
  }
  if (stepsPerSecond > kMaxStepRateSps) {
    stepsPerSecond = kMaxStepRateSps;
  }
  uint32_t inc = seleno::phaseIncrementForRate(stepsPerSecond, kStepTickHz);
  portENTER_CRITICAL(&g_mux);
  g_axes[axis].rateSps = stepsPerSecond;
  g_axes[axis].phaseInc = inc;
  portEXIT_CRITICAL(&g_mux);
}

double stepperRate(int axis) {
  return validAxis(axis) ? g_axes[axis].rateSps : 0.0;
}

void stepperRequestStop(int axis, MotionEnd reason) {
  if (!validAxis(axis)) {
    return;
  }
  portENTER_CRITICAL_SAFE(&g_mux);
  if (g_axes[axis].running) {
    g_axes[axis].stopRequest = reason;
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

int32_t stepperPosition(int axis) {
  if (!validAxis(axis)) {
    return 0;
  }
  portENTER_CRITICAL(&g_mux);
  int32_t p = g_axes[axis].position;
  portEXIT_CRITICAL(&g_mux);
  return p;
}

void stepperSetPosition(int axis, int32_t position) {
  if (!validAxis(axis)) {
    return;
  }
  portENTER_CRITICAL(&g_mux);
  g_axes[axis].position = position;
  portEXIT_CRITICAL(&g_mux);
}

int32_t stepperStepsRemaining(int axis) {
  if (!validAxis(axis)) {
    return 0;
  }
  portENTER_CRITICAL(&g_mux);
  int32_t r = g_axes[axis].running ? g_axes[axis].stepsRemaining : 0;
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
