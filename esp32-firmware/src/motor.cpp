#include "motor.h"
#include <cmath>
#include <esp_timer.h>

MotorEntry g_motors[kMaxMotors] = {};

namespace {
struct StepParams {
  Motor* motor;
  uint32_t stepsNeeded;  // Unused by the continuous task
  bool forward;
};
}  // namespace

Motor::Motor(int step, int dir, int en, uint32_t stepsPerRev)
    : stepPin(step), dirPin(dir), enPin(en), enabled(false), moving(false),
      stopRequested(false), position(0), stepsPerRevolution(stepsPerRev),
      stepDelayUs(1250), stepTaskHandle(nullptr) {
}

void Motor::initialize() {
  pinMode(stepPin, OUTPUT);
  pinMode(dirPin, OUTPUT);
  pinMode(enPin, OUTPUT);
  digitalWrite(stepPin, LOW);
  digitalWrite(dirPin, LOW);
  digitalWrite(enPin, HIGH);  // Disabled by default
}

void Motor::cleanup() {
  if (moving) {
    // Ask the step task to exit on its own. It checks stopRequested once per
    // step, so it finishes within one step period. Killing it from here
    // instead would risk deleting a task that has already deleted itself.
    stopRequested = true;
    uint32_t timeoutMs = stepDelayUs / 1000U + 100U;
    uint32_t startMs = millis();
    while (moving && (millis() - startMs) < timeoutMs) {
      delay(1);
    }
    if (moving && stepTaskHandle != nullptr) {
      // Task is still alive (moving is cleared as its last action), so the
      // handle is valid.
      vTaskDelete(stepTaskHandle);
      moving = false;
    }
    stepTaskHandle = nullptr;
  }
  digitalWrite(enPin, HIGH);  // Disable driver
  digitalWrite(stepPin, LOW);
  digitalWrite(dirPin, LOW);
}

void Motor::setSpeed(uint32_t delayUs) {
  stepDelayUs = delayUs;
}

void Motor::engage() {
  digitalWrite(enPin, LOW);  // Active low
  enabled = true;
}

void Motor::disengage() {
  digitalWrite(enPin, HIGH);  // Disable
  enabled = false;
}

bool Motor::isEnabled() const {
  return enabled;
}

bool Motor::isMoving() const {
  return moving;
}

uint32_t Motor::getStepDelayUs() const {
  return stepDelayUs;
}

uint32_t Motor::getStepsPerRevolution() const {
  return stepsPerRevolution;
}

int32_t Motor::getPosition() const {
  return position;
}

void Motor::resetPosition() {
  position = 0;
}

void Motor::stop() {
  if (!moving) {
    return;
  }
  stopRequested = true;
}

void Motor::turnDegrees(float degrees, bool forward) {
  if (!enabled) {
    return;
  }
  if (moving) {
    return;  // Already moving, ignore
  }

  // Negative angles turn the other way; converting a negative float to an
  // unsigned step count is undefined behaviour.
  if (degrees < 0.0f) {
    degrees = -degrees;
    forward = !forward;
  }
  if (!std::isfinite(degrees) || degrees == 0.0f) {
    return;
  }

  double steps = (degrees / 360.0) * stepsPerRevolution;
  uint32_t stepsNeeded = steps >= (double)UINT32_MAX ? UINT32_MAX : (uint32_t)steps;
  if (stepsNeeded == 0) {
    return;
  }

  stopRequested = false;
  moving = true;
  digitalWrite(dirPin, forward ? HIGH : LOW);

  StepParams* params = new StepParams{this, stepsNeeded, forward};

  if (xTaskCreatePinnedToCore(
        stepTask,
        "MotorStep",
        2048,
        params,
        1,
        &stepTaskHandle,
        1
      ) != pdPASS) {
    delete params;
    stepTaskHandle = nullptr;
    moving = false;
  }
}

void Motor::startContinuous(bool forward) {
  if (!enabled) {
    return;
  }
  if (moving) {
    return;  // Already moving, ignore
  }

  stopRequested = false;
  moving = true;

  digitalWrite(dirPin, forward ? HIGH : LOW);

  StepParams* params = new StepParams{this, 0, forward};

  if (xTaskCreatePinnedToCore(
        continuousStepTask,
        "MotorContinuous",
        2048,
        params,
        1,
        &stepTaskHandle,
        1
      ) != pdPASS) {
    delete params;
    stepTaskHandle = nullptr;
    moving = false;
  }
}

void Motor::stepTask(void* pvParameters) {
  StepParams* params = (StepParams*)pvParameters;
  Motor* motor = params->motor;
  uint32_t stepsNeeded = params->stepsNeeded;
  bool forward = params->forward;
  delete params;

  int32_t positionDelta = forward ? 1 : -1;

  for (uint32_t i = 0; i < stepsNeeded; i++) {
    if (motor->stopRequested) {
      break;
    }

    digitalWrite(motor->stepPin, HIGH);
    delayMicroseconds(PULSE_WIDTH_US);
    digitalWrite(motor->stepPin, LOW);
    delayMicroseconds(motor->stepDelayUs - PULSE_WIDTH_US);

    motor->position += positionDelta;
  }

  // Clearing moving must be the last access to motor: once it is false,
  // cleanup() may delete the Motor.
  motor->stepTaskHandle = nullptr;
  motor->moving = false;
  vTaskDelete(nullptr);
}

void Motor::continuousStepTask(void* pvParameters) {
  StepParams* params = (StepParams*)pvParameters;
  Motor* motor = params->motor;
  int32_t positionDelta = params->forward ? 1 : -1;
  delete params;

  // Schedule steps against absolute deadlines so scheduling latency and loop
  // overhead don't accumulate. This is the sidereal tracking path, where a
  // relative delay per step would make the mount drift slow over time.
  int64_t nextStepUs = esp_timer_get_time();

  while (!motor->stopRequested) {
    digitalWrite(motor->stepPin, HIGH);
    delayMicroseconds(PULSE_WIDTH_US);
    digitalWrite(motor->stepPin, LOW);

    motor->position += positionDelta;

    nextStepUs += motor->stepDelayUs;
    int64_t remainingUs = nextStepUs - esp_timer_get_time();
    if (remainingUs < -(int64_t)motor->stepDelayUs) {
      // Fell more than a full step behind (e.g. speed changed); resync rather
      // than bursting to catch up.
      nextStepUs = esp_timer_get_time();
      continue;
    }
    // Sleep through the bulk of long periods instead of busy-waiting, which
    // would starve loop() on this core; spin only for the final stretch.
    if (remainingUs > 3000) {
      vTaskDelay(pdMS_TO_TICKS((remainingUs - 2000) / 1000));
    }
    while (esp_timer_get_time() < nextStepUs && !motor->stopRequested) {
    }
  }

  // Clearing moving must be the last access to motor: once it is false,
  // cleanup() may delete the Motor.
  motor->stepTaskHandle = nullptr;
  motor->moving = false;
  vTaskDelete(nullptr);
}

Motor* findMotor(const String& id) {
  for (size_t i = 0; i < kMaxMotors; i++) {
    if (g_motors[i].motor != nullptr && g_motors[i].id == id) {
      return g_motors[i].motor;
    }
  }
  return nullptr;
}

bool registerMotor(const String& id, Motor* motor, bool owned) {
  if (id.length() == 0 || motor == nullptr) {
    return false;
  }
  if (findMotor(id) != nullptr) {
    return false;
  }
  for (size_t i = 0; i < kMaxMotors; i++) {
    if (g_motors[i].motor == nullptr) {
      g_motors[i].id = id;
      g_motors[i].motor = motor;
      g_motors[i].owned = owned;
      return true;
    }
  }
  return false;
}
