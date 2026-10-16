#ifndef MOTOR_H
#define MOTOR_H

#include <Arduino.h>
#include <cstdint>

// Stepper motor specifications
#define STEPS_PER_REVOLUTION 1600  // 200 * 8 microsteps
#define PULSE_WIDTH_US 10          // Minimum for TMC2209

class Motor {
private:
  bool enabled;
  volatile bool stopRequested;
  volatile int32_t position;  // Position tracker in steps (signed for direction)

public:
  int stepPin;
  int dirPin;
  int enPin;
  volatile bool moving;  // Written by the step task, read from the main loop
  uint32_t stepsPerRevolution;
  uint32_t stepDelayUs;
  TaskHandle_t stepTaskHandle;

  Motor(int step, int dir, int en, uint32_t stepsPerRev = STEPS_PER_REVOLUTION);

  void initialize();
  void cleanup();
  void setSpeed(uint32_t delayUs);
  void engage();
  void disengage();
  void stop();

  bool isEnabled() const;
  bool isMoving() const;
  uint32_t getStepDelayUs() const;
  uint32_t getStepsPerRevolution() const;
  int32_t getPosition() const;
  void resetPosition();

  void turnDegrees(float degrees, bool forward = true);
  void startContinuous(bool forward = true);

private:
  static void stepTask(void* pvParameters);
  static void continuousStepTask(void* pvParameters);
};

struct MotorEntry {
  String id;
  Motor* motor;
  bool owned;
};

constexpr size_t kMaxMotors = 8;
extern MotorEntry g_motors[kMaxMotors];

Motor* findMotor(const String& id);
bool registerMotor(const String& id, Motor* motor, bool owned);

#endif // MOTOR_H
