// Motor and TMC2209 driver commands.
#include <Arduino.h>
#include <cmath>

#include "commands.h"
#include "config.h"
#include "host_link.h"
#include "logic/step_math.h"
#include "motors.h"
#include "tmc_drivers.h"

namespace {

void sendMotorStatus(int axis) {
  JsonDocument resp;
  motorStatusJson(axis, resp["data"].to<JsonObject>());
  hostSendResponse(resp);
}

bool reportResult(bool ok, const char* error) {
  if (!ok) {
    hostSendError(error != nullptr ? error : "Failed");
  }
  return ok;
}

// "microsteps": a power of two (fixed) or "auto". present=false if absent.
// out = 0 means automatic.
bool requestedMicrosteps(JsonDocument& req, bool& present, uint16_t& out, const char** error) {
  present = !req["microsteps"].isNull();
  out = 0;
  if (!present) {
    return true;
  }
  if (req["microsteps"].is<const char*>()) {
    if (strcasecmp(req["microsteps"].as<const char*>(), "auto") == 0) {
      return true;
    }
    *error = "microsteps must be a power of two from 1 to 256, or \"auto\"";
    return false;
  }
  uint32_t m = req["microsteps"].as<uint32_t>();
  if (!seleno::isValidMicrosteps(m)) {
    *error = "microsteps must be a power of two from 1 to 256, or \"auto\"";
    return false;
  }
  out = static_cast<uint16_t>(m);
  return true;
}

// "steps_per_rev" (logical units for positions and speeds) as a logical
// microstep count: 200 x a power of two. 0 if absent.
bool requestedResolution(JsonDocument& req, uint16_t& out, const char** error) {
  out = 0;
  if (req["steps_per_rev"].isNull()) {
    return true;
  }
  uint32_t spr = req["steps_per_rev"].as<uint32_t>();
  if (spr == 0 || spr % kFullStepsPerRev != 0 || !seleno::isValidMicrosteps(spr / kFullStepsPerRev)) {
    *error = "steps_per_rev must be 200 x a power of two (200-51200)";
    return false;
  }
  out = static_cast<uint16_t>(spr / kFullStepsPerRev);
  return true;
}

bool applySpeedFields(int axis, JsonDocument& req, bool& sawSpeed, const char** error) {
  sawSpeed = false;
  if (!req["speed_us"].isNull()) {
    sawSpeed = true;
    return motorSetSpeedUs(axis, req["speed_us"].as<uint32_t>(), error);
  }
  if (!req["sps"].isNull()) {
    sawSpeed = true;
    return motorSetSpeedSps(axis, req["sps"].as<double>(), error);
  }
  return true;
}

void handleCreateMotor(JsonDocument& req) {
  const char* id = req["motor"] | "";
  if (id[0] == '\0') {
    hostSendError("Missing motor id");
    return;
  }

  int axis = -1;
  bool havePins = !req["step"].isNull() || !req["dir"].isNull() || !req["en"].isNull();
  if (havePins) {
    if (req["step"].isNull() || req["dir"].isNull() || req["en"].isNull()) {
      hostSendError("Missing motor pins");
      return;
    }
    axis = motorAxisForPins(req["step"].as<int>(), req["dir"].as<int>(), req["en"].as<int>());
    if (axis < 0) {
      hostSendError("Pins do not match a motor axis on this board");
      return;
    }
  } else if (!req["axis"].isNull()) {
    axis = req["axis"].as<int>();
    if (axis < 0 || axis >= kAxisCount) {
      hostSendError("Invalid axis");
      return;
    }
  } else {
    axis = motorAxisById(id);
    if (axis < 0) {
      hostSendError("Unknown motor; give its pins or axis");
      return;
    }
  }

  const char* error = nullptr;
  bool haveMicrosteps = false;
  uint16_t microsteps = 0;
  uint16_t resolution = 0;
  if (!requestedMicrosteps(req, haveMicrosteps, microsteps, &error) ||
      !requestedResolution(req, resolution, &error)) {
    hostSendError(error);
    return;
  }
  if (!reportResult(motorRegister(axis, id, &error), error)) {
    return;
  }
  if (resolution != 0 && resolution != motorLogicalMicrosteps(axis)) {
    if (!reportResult(motorSetResolution(axis, resolution, &error), error)) {
      return;
    }
  }
  if (haveMicrosteps && !reportResult(motorSetMicrosteps(axis, microsteps, &error), error)) {
    return;
  }
  bool sawSpeed = false;
  if (!reportResult(applySpeedFields(axis, req, sawSpeed, &error), error)) {
    return;
  }
  if (!req["engage"].isNull()) {
    if (!reportResult(motorEngage(axis, req["engage"].as<bool>(), &error), error)) {
      return;
    }
  }
  sendMotorStatus(axis);
}

void handleDeleteMotor(JsonDocument& req) {
  const char* id = req["motor"] | "";
  if (id[0] == '\0') {
    hostSendError("Missing motor id");
    return;
  }
  int axis = motorAxisById(id);
  if (axis < 0) {
    hostSendError("Motor not found");
    return;
  }
  motorUnregister(axis);
  hostSendOk();
}

void handleListMotors() {
  JsonDocument resp;
  JsonObject data = resp["data"].to<JsonObject>();
  JsonArray motors = data["motors"].to<JsonArray>();
  for (int i = 0; i < kAxisCount; i++) {
    if (motorIdForAxis(i) != nullptr) {
      motors.add(motorIdForAxis(i));
    }
  }
  hostSendResponse(resp);
}

void handleDriver(int axis, JsonDocument& req) {
  const char* action = req["action"] | "status";
  const char* error = nullptr;

  if (strcmp(action, "status") == 0) {
    JsonDocument resp;
    driverStatusJson(axis, resp["data"].to<JsonObject>());
    hostSendResponse(resp);
    return;
  }
  if (strcmp(action, "reinit") == 0) {
    if (!driverReinit(axis)) {
      hostSendError("Driver not responding on TMC UART");
      return;
    }
    hostSendOk();
    return;
  }
  if (strcmp(action, "read_reg") == 0) {
    if (req["reg"].isNull()) {
      hostSendError("Missing reg");
      return;
    }
    uint8_t reg = req["reg"].as<uint8_t>();
    uint32_t value = 0;
    if (!driverReadRegister(axis, reg, value)) {
      hostSendError("Register read failed");
      return;
    }
    JsonDocument resp;
    JsonObject data = resp["data"].to<JsonObject>();
    data["reg"] = reg;
    data["value"] = value;
    hostSendResponse(resp);
    return;
  }
  if (strcmp(action, "write_reg") == 0) {
    if (req["reg"].isNull() || req["value"].isNull()) {
      hostSendError("Missing reg or value");
      return;
    }
    if (!driverWriteRegister(axis, req["reg"].as<uint8_t>(), req["value"].as<uint32_t>())) {
      hostSendError("Driver not responding on TMC UART");
      return;
    }
    hostSendOk();
    return;
  }
  if (strcmp(action, "set") == 0) {
    if (!req["interpolate"].isNull() &&
        !reportResult(driverSetInterpolation(axis, req["interpolate"].as<bool>(), &error), error)) {
      return;
    }
    if (!req["stall_threshold"].isNull() &&
        !reportResult(driverSetStallThreshold(axis, req["stall_threshold"].as<uint8_t>(), &error), error)) {
      return;
    }
    if (!req["invert_direction"].isNull() &&
        !reportResult(driverSetInvertDirection(axis, req["invert_direction"].as<bool>(), &error), error)) {
      return;
    }
    JsonDocument resp;
    driverStatusJson(axis, resp["data"].to<JsonObject>());
    hostSendResponse(resp);
    return;
  }
  hostSendError("Unknown driver action");
}

void handleSetCurrent(int axis, JsonDocument& req) {
  const DriverSettings& s = driverSettings(axis);
  uint32_t runMa = req["run_ma"] | static_cast<uint32_t>(s.runCurrentMa);
  float hold = s.holdMultiplier;
  if (!req["hold_multiplier"].isNull()) {
    hold = req["hold_multiplier"].as<float>();
  } else if (!req["hold_ma"].isNull()) {
    hold = runMa > 0 ? req["hold_ma"].as<float>() / runMa : 0.0f;
  }
  const char* error = nullptr;
  if (!reportResult(driverSetCurrent(axis, runMa > 0xFFFF ? 0xFFFF : runMa, hold, &error), error)) {
    return;
  }
  JsonDocument resp;
  driverStatusJson(axis, resp["data"].to<JsonObject>());
  hostSendResponse(resp);
}

void handleSetChopper(int axis, JsonDocument& req) {
  ChopperMode mode;
  if (!parseChopperMode(req["mode"] | "", mode)) {
    hostSendError("mode must be auto, stealthchop, spreadcycle or hybrid");
    return;
  }
  // Threshold in full steps/s, or in the host's logical steps/s.
  double threshold = req["hybrid_threshold_fsps"] | 0.0;
  if (threshold <= 0.0 && !req["hybrid_threshold_sps"].isNull()) {
    threshold = req["hybrid_threshold_sps"].as<double>() / motorLogicalMicrosteps(axis);
  }
  const char* error = nullptr;
  if (!reportResult(driverSetChopper(axis, mode, threshold, &error), error)) {
    return;
  }
  sendMotorStatus(axis);
}

}  // namespace

bool handleMotorCommand(const char* cmd, JsonDocument& req) {
  if (strcmp(cmd, "create_motor") == 0) {
    handleCreateMotor(req);
    return true;
  }
  if (strcmp(cmd, "delete_motor") == 0) {
    handleDeleteMotor(req);
    return true;
  }
  if (strcmp(cmd, "list_motors") == 0) {
    handleListMotors();
    return true;
  }

  static const char* const kMotorCommands[] = {
      "engage", "disengage", "enable", "set_speed", "turn_degrees", "move_steps",
      "start_continuous", "stop", "status", "get_position", "reset_position", "set_position",
      "set_microsteps", "set_auto", "set_steps_per_rev", "set_accel", "set_chopper", "set_current", "driver",
  };
  bool known = false;
  for (const char* name : kMotorCommands) {
    if (strcmp(cmd, name) == 0) {
      known = true;
      break;
    }
  }
  if (!known) {
    return false;
  }

  const char* id = req["motor"] | "";
  if (id[0] == '\0') {
    hostSendError("Missing motor id");
    return true;
  }
  int axis = motorAxisById(id);
  if (axis < 0) {
    hostSendError("Unknown motor id");
    return true;
  }

  const char* error = nullptr;
  if (strcmp(cmd, "engage") == 0) {
    if (reportResult(motorEngage(axis, true, &error), error)) hostSendOk();
  } else if (strcmp(cmd, "disengage") == 0) {
    if (reportResult(motorEngage(axis, false, &error), error)) hostSendOk();
  } else if (strcmp(cmd, "enable") == 0) {
    if (reportResult(motorEngage(axis, req["value"] | false, &error), error)) hostSendOk();
  } else if (strcmp(cmd, "set_speed") == 0) {
    bool sawSpeed = false;
    if (!reportResult(applySpeedFields(axis, req, sawSpeed, &error), error)) return true;
    if (!sawSpeed) {
      hostSendError("Missing speed value");
      return true;
    }
    hostSendOk();
  } else if (strcmp(cmd, "turn_degrees") == 0) {
    if (req["degrees"].isNull()) {
      hostSendError("Missing degrees");
      return true;
    }
    bool forward = req["forward"] | true;
    if (reportResult(motorTurnDegrees(axis, req["degrees"].as<float>(), forward, &error), error)) hostSendOk();
  } else if (strcmp(cmd, "move_steps") == 0) {
    if (req["steps"].isNull()) {
      hostSendError("Missing steps");
      return true;
    }
    if (reportResult(motorMoveSteps(axis, req["steps"].as<int32_t>(), &error), error)) hostSendOk();
  } else if (strcmp(cmd, "start_continuous") == 0) {
    bool forward = req["forward"] | true;
    if (reportResult(motorStartContinuous(axis, forward, &error), error)) hostSendOk();
  } else if (strcmp(cmd, "stop") == 0) {
    motorStop(axis);
    hostSendOk();
  } else if (strcmp(cmd, "status") == 0) {
    sendMotorStatus(axis);
  } else if (strcmp(cmd, "get_position") == 0) {
    JsonDocument resp;
    JsonObject data = resp["data"].to<JsonObject>();
    data["motor"] = id;
    data["position"] = motorPosition(axis);
    data["position_fine"] = motorPosition256(axis);
    data["steps_per_rev"] = motorStepsPerRev(axis);
    data["moving"] = stepperIsMoving(axis);
    hostSendResponse(resp);
  } else if (strcmp(cmd, "reset_position") == 0) {
    motorSetPosition(axis, 0);
    hostSendOk();
  } else if (strcmp(cmd, "set_position") == 0) {
    if (req["position"].isNull()) {
      hostSendError("Missing position");
      return true;
    }
    motorSetPosition(axis, req["position"].as<int64_t>());
    hostSendOk();
  } else if (strcmp(cmd, "set_microsteps") == 0) {
    bool present = false;
    uint16_t microsteps = 0;
    if (!requestedMicrosteps(req, present, microsteps, &error)) {
      hostSendError(error);
      return true;
    }
    if (!present) {
      hostSendError("Missing microsteps");
      return true;
    }
    if (reportResult(motorSetMicrosteps(axis, microsteps, &error), error)) sendMotorStatus(axis);
  } else if (strcmp(cmd, "set_auto") == 0) {
    AutoMicrostepParams params = motorAutoParams(axis);
    if (!req["min_microsteps"].isNull()) params.minMicrosteps = req["min_microsteps"].as<uint16_t>();
    if (!req["max_microsteps"].isNull()) params.maxMicrosteps = req["max_microsteps"].as<uint16_t>();
    if (!req["max_pulse_hz"].isNull()) params.maxPulseHz = req["max_pulse_hz"].as<float>();
    if (reportResult(motorSetAutoParams(axis, params, &error), error)) sendMotorStatus(axis);
  } else if (strcmp(cmd, "set_accel") == 0) {
    double accel = req["accel"] | motorAccel(axis);
    double startSps = req["start_sps"] | motorStartSpeed(axis);
    if (reportResult(motorSetAccel(axis, accel, startSps, &error), error)) sendMotorStatus(axis);
  } else if (strcmp(cmd, "set_steps_per_rev") == 0) {
    uint16_t resolution = 0;
    if (!requestedResolution(req, resolution, &error)) {
      hostSendError(error);
      return true;
    }
    if (resolution == 0) {
      hostSendError("Missing steps_per_rev");
      return true;
    }
    if (reportResult(motorSetResolution(axis, resolution, &error), error)) sendMotorStatus(axis);
  } else if (strcmp(cmd, "set_chopper") == 0) {
    handleSetChopper(axis, req);
  } else if (strcmp(cmd, "set_current") == 0) {
    handleSetCurrent(axis, req);
  } else if (strcmp(cmd, "driver") == 0) {
    handleDriver(axis, req);
  }
  return true;
}
