#include "tmc_drivers.h"

#include <Arduino.h>
#include <TMCStepper.h>

#include "board_pins.h"
#include "config.h"
#include "host_link.h"
#include "logic/step_math.h"
#include "motors.h"
#include "stepper.h"

namespace {

// Exposes the library's protected raw register access for bring-up.
class SelenoTmc : public TMC2209Stepper {
 public:
  SelenoTmc(Stream* port, float rsense, uint8_t addr) : TMC2209Stepper(port, rsense, addr) {}
  uint32_t readReg(uint8_t reg) { return read(reg); }
  void writeReg(uint8_t reg, uint32_t value) { write(reg, value); }
};

// Register addresses (TMC2209 datasheet)
constexpr uint8_t REG_GCONF = 0x00;
constexpr uint8_t REG_GSTAT = 0x01;
constexpr uint8_t REG_IFCNT = 0x02;
constexpr uint8_t REG_IOIN = 0x06;
constexpr uint8_t REG_TSTEP = 0x12;
constexpr uint8_t REG_SG_RESULT = 0x41;
constexpr uint8_t REG_MSCNT = 0x6A;
constexpr uint8_t REG_CHOPCONF = 0x6C;
constexpr uint8_t REG_DRV_STATUS = 0x6F;
constexpr uint8_t REG_PWM_SCALE = 0x71;

constexpr uint8_t kTmc2209Version = 0x21;

// GCONF bits
constexpr uint32_t GCONF_I_SCALE_ANALOG = 1u << 0;
constexpr uint32_t GCONF_EN_SPREADCYCLE = 1u << 2;
constexpr uint32_t GCONF_SHAFT = 1u << 3;
constexpr uint32_t GCONF_PDN_DISABLE = 1u << 6;
constexpr uint32_t GCONF_MSTEP_REG_SELECT = 1u << 7;
constexpr uint32_t GCONF_MULTISTEP_FILT = 1u << 8;

// GSTAT bits
constexpr uint8_t GSTAT_RESET = 1u << 0;
constexpr uint8_t GSTAT_DRV_ERR = 1u << 1;
constexpr uint8_t GSTAT_UV_CP = 1u << 2;

constexpr uint32_t kReprobeIntervalMs = 5000;

struct DriverState {
  SelenoTmc* tmc;
  int enPin;
  int diagPin;
  bool present;
  bool configured;
  bool enabled;
  bool diagHigh;
  bool otpw;
  bool ot;
  uint8_t version;
  uint32_t uartErrors;
  uint32_t lastReprobeMs;
  DriverSettings settings;
};

SelenoTmc g_tmc0(&Serial2, TMC_RSENSE, kTmcAddrMotor1);
SelenoTmc g_tmc1(&Serial2, TMC_RSENSE, kTmcAddrMotor2);

DriverState g_drivers[kAxisCount];
uint32_t g_lastPollMs = 0;
int g_pollAxis = 0;

DriverSettings defaultSettings() {
  DriverSettings s;
  s.microsteps = kDefaultMicrosteps;
  s.runCurrentMa = kDefaultRunCurrentMa;
  s.holdMultiplier = kDefaultHoldMultiplier;
  s.chopper = ChopperMode::StealthChop;
  s.hybridThresholdSps = 0.0;
  s.interpolate = true;
  s.stallThreshold = 0;
  s.invertDirection = false;
  return s;
}

bool validAxis(int axis) {
  return axis >= 0 && axis < kAxisCount;
}

// Reads a register and reports whether the driver actually answered.
bool readChecked(DriverState& d, uint8_t reg, uint32_t& out) {
  out = d.tmc->readReg(reg);
  if (d.tmc->CRCerror) {
    d.uartErrors++;
    return false;
  }
  return true;
}

bool probe(DriverState& d) {
  uint32_t ioin = 0;
  if (!readChecked(d, REG_IOIN, ioin)) {
    d.version = 0;
    return false;
  }
  d.version = static_cast<uint8_t>(ioin >> 24);
  return d.version == kTmc2209Version;
}

uint32_t gconfFor(const DriverSettings& s) {
  uint32_t g = GCONF_I_SCALE_ANALOG | GCONF_PDN_DISABLE | GCONF_MSTEP_REG_SELECT | GCONF_MULTISTEP_FILT;
  if (s.chopper == ChopperMode::SpreadCycle) {
    g |= GCONF_EN_SPREADCYCLE;
  }
  if (s.invertDirection) {
    g |= GCONF_SHAFT;
  }
  return g;
}

uint32_t tpwmthrsFor(const DriverSettings& s) {
  if (s.chopper != ChopperMode::Hybrid) {
    return 0;
  }
  return seleno::tpwmthrsForRate(s.hybridThresholdSps, s.microsteps);
}

// Write every setting, then read GCONF and CHOPCONF back to confirm they
// landed (report: configure fully before pulling ENN low).
bool applySettings(DriverState& d) {
  SelenoTmc& t = *d.tmc;
  const DriverSettings& s = d.settings;

  t.senddelay(2);  // two drivers on one wire: report asks for SENDDELAY >= 2
  t.GCONF(gconfFor(s));
  t.toff(4);
  t.microsteps(s.microsteps);
  t.intpol(s.interpolate);
  t.iholddelay(6);
  t.rms_current(s.runCurrentMa, s.holdMultiplier);
  t.pwm_autoscale(true);
  t.pwm_autograd(true);
  t.TPWMTHRS(tpwmthrsFor(s));
  t.SGTHRS(s.stallThreshold);
  // StallGuard only drives DIAG while TCOOLTHRS >= TSTEP.
  t.TCOOLTHRS(s.stallThreshold > 0 ? 0xFFFFF : 0);
  t.GSTAT(GSTAT_RESET | GSTAT_DRV_ERR | GSTAT_UV_CP);  // write 1 to clear

  uint32_t gconf = 0;
  uint32_t chopconf = 0;
  if (!readChecked(d, REG_GCONF, gconf) || !readChecked(d, REG_CHOPCONF, chopconf)) {
    return false;
  }
  uint32_t wantGconf = gconfFor(s);
  uint32_t gconfMask = GCONF_I_SCALE_ANALOG | GCONF_EN_SPREADCYCLE | GCONF_SHAFT |
                       GCONF_PDN_DISABLE | GCONF_MSTEP_REG_SELECT | GCONF_MULTISTEP_FILT;
  uint8_t mres = (chopconf >> 24) & 0x0F;
  return (gconf & gconfMask) == (wantGconf & gconfMask) &&
         mres == seleno::mresFromMicrosteps(s.microsteps);
}

bool configure(DriverState& d) {
  d.present = probe(d);
  d.configured = d.present && applySettings(d);
  return d.configured;
}

void writeEnablePin(DriverState& d, bool on) {
  digitalWrite(d.enPin, on ? LOW : HIGH);  // ENN is active low
  d.enabled = on;
}

const char* axisMotorId(int axis) {
  const char* id = motorIdForAxis(axis);
  return id != nullptr ? id : "";
}

void addDrvStatusFlags(JsonObject out, uint32_t drv) {
  out["otpw"] = (drv & (1u << 0)) != 0;
  out["ot"] = (drv & (1u << 1)) != 0;
  out["s2ga"] = (drv & (1u << 2)) != 0;
  out["s2gb"] = (drv & (1u << 3)) != 0;
  out["s2vsa"] = (drv & (1u << 4)) != 0;
  out["s2vsb"] = (drv & (1u << 5)) != 0;
  out["ola"] = (drv & (1u << 6)) != 0;
  out["olb"] = (drv & (1u << 7)) != 0;
  out["t120"] = (drv & (1u << 8)) != 0;
  out["t143"] = (drv & (1u << 9)) != 0;
  out["t150"] = (drv & (1u << 10)) != 0;
  out["t157"] = (drv & (1u << 11)) != 0;
  out["cs_actual"] = (drv >> 16) & 0x1F;
  out["stealth"] = (drv & (1u << 30)) != 0;
  out["standstill"] = (drv & (1u << 31)) != 0;
}

void emitDriverEvent(int axis, const char* name, uint32_t drvStatus, bool haveStatus) {
  JsonDocument ev;
  ev["event"] = name;
  ev["motor"] = axisMotorId(axis);
  ev["axis"] = axis;
  if (haveStatus) {
    addDrvStatusFlags(ev["drv_status"].to<JsonObject>(), drvStatus);
  }
  hostEmitEvent(ev);
}

void pollDiag(int axis) {
  DriverState& d = g_drivers[axis];
  // DIAG floats when no driver answered (e.g. on the dev kit), so only
  // trust it for a driver that's really there.
  if (!d.present) {
    d.diagHigh = false;
    return;
  }
  bool high = digitalRead(d.diagPin) == HIGH;
  if (high == d.diagHigh) {
    return;
  }
  d.diagHigh = high;
  uint32_t drv = 0;
  bool haveStatus = readChecked(d, REG_DRV_STATUS, drv);
  if (high) {
    // The driver has shut its bridge or stalled: counting further steps
    // would corrupt the position, so stop the axis.
    stepperRequestStop(axis, MotionEnd::DriverFault);
    emitDriverEvent(axis, "driver_fault", drv, haveStatus);
  } else {
    emitDriverEvent(axis, "driver_fault_cleared", drv, haveStatus);
  }
}

void pollRegisters(int axis) {
  DriverState& d = g_drivers[axis];
  uint32_t now = millis();

  if (!d.present) {
    if (now - d.lastReprobeMs < kReprobeIntervalMs) {
      return;
    }
    d.lastReprobeMs = now;
    if (configure(d)) {
      emitDriverEvent(axis, "driver_found", 0, false);
    }
    return;
  }

  uint32_t gstat = 0;
  if (!readChecked(d, REG_GSTAT, gstat)) {
    // A failed read can be line noise; a failed re-probe as well means the
    // driver is gone.
    if (!probe(d)) {
      d.present = false;
      d.configured = false;
      d.lastReprobeMs = now;
      stepperRequestStop(axis, MotionEnd::DriverFault);
      emitDriverEvent(axis, "driver_lost", 0, false);
    }
    return;
  }

  if (gstat & GSTAT_RESET) {
    // A brown-out put the driver back on its OTP/strap defaults (wrong
    // microsteps, PDN_UART as power-down). Stop, reconfigure, report.
    stepperRequestStop(axis, MotionEnd::DriverFault);
    configure(d);
    emitDriverEvent(axis, "driver_reset", 0, false);
    return;
  }
  if (gstat & (GSTAT_DRV_ERR | GSTAT_UV_CP)) {
    d.tmc->GSTAT(gstat & (GSTAT_DRV_ERR | GSTAT_UV_CP));
  }

  uint32_t drv = 0;
  if (!readChecked(d, REG_DRV_STATUS, drv)) {
    return;
  }
  bool otpw = (drv & 1u) != 0;
  bool ot = (drv & 2u) != 0;
  if (otpw != d.otpw || ot != d.ot) {
    d.otpw = otpw;
    d.ot = ot;
    emitDriverEvent(axis, "driver_temperature", drv, true);
  }
}

bool requireAxis(int axis, const char** error) {
  if (!validAxis(axis)) {
    if (error) *error = "Invalid axis";
    return false;
  }
  return true;
}

// Re-write settings to a live driver. A driver that isn't answering keeps
// the new settings for when it comes back.
bool commitSettings(int axis, const char** error) {
  DriverState& d = g_drivers[axis];
  if (!d.present) {
#if SELENO_DEVKIT
    return true;
#else
    if (error) *error = "Driver not responding on TMC UART";
    return false;
#endif
  }
  d.configured = applySettings(d);
  if (!d.configured) {
    if (error) *error = "Driver did not accept configuration";
    return false;
  }
  return true;
}

}  // namespace

const char* chopperModeName(ChopperMode mode) {
  switch (mode) {
    case ChopperMode::SpreadCycle: return "spreadcycle";
    case ChopperMode::Hybrid: return "hybrid";
    default: return "stealthchop";
  }
}

bool parseChopperMode(const char* text, ChopperMode& out) {
  if (text == nullptr) {
    return false;
  }
  if (strcasecmp(text, "stealthchop") == 0 || strcasecmp(text, "stealth") == 0 ||
      strcasecmp(text, "silent") == 0) {
    out = ChopperMode::StealthChop;
    return true;
  }
  if (strcasecmp(text, "spreadcycle") == 0 || strcasecmp(text, "spread") == 0) {
    out = ChopperMode::SpreadCycle;
    return true;
  }
  if (strcasecmp(text, "hybrid") == 0) {
    out = ChopperMode::Hybrid;
    return true;
  }
  return false;
}

void driversBegin() {
  const int enPins[kAxisCount] = {PIN_M1_EN, PIN_M2_EN};
  const int diagPins[kAxisCount] = {PIN_M1_DIAG, PIN_M2_DIAG};
  SelenoTmc* tmcs[kAxisCount] = {&g_tmc0, &g_tmc1};

  // Drivers off before anything else; R35/R36 already hold ENN high.
  for (int i = 0; i < kAxisCount; i++) {
    pinMode(enPins[i], OUTPUT);
    digitalWrite(enPins[i], HIGH);
    pinMode(diagPins[i], INPUT);  // IO34/35 are input-only, no internal pulls
  }

  Serial2.begin(kTmcBaud, SERIAL_8N1, PIN_TMC_RX, PIN_TMC_TX);

  for (int i = 0; i < kAxisCount; i++) {
    DriverState& d = g_drivers[i];
    d = DriverState();
    d.tmc = tmcs[i];
    d.enPin = enPins[i];
    d.diagPin = diagPins[i];
    d.settings = defaultSettings();
    d.lastReprobeMs = millis();
    configure(d);
  }
}

bool driverPresent(int axis) {
  return validAxis(axis) && g_drivers[axis].present;
}

const DriverSettings& driverSettings(int axis) {
  static DriverSettings fallback = defaultSettings();
  return validAxis(axis) ? g_drivers[axis].settings : fallback;
}

bool driverSetMicrosteps(int axis, uint16_t microsteps, const char** error) {
  if (!requireAxis(axis, error)) return false;
  if (!seleno::isValidMicrosteps(microsteps)) {
    if (error) *error = "Microsteps must be a power of two from 1 to 256";
    return false;
  }
  DriverSettings old = g_drivers[axis].settings;
  g_drivers[axis].settings.microsteps = microsteps;
  if (!commitSettings(axis, error)) {
    g_drivers[axis].settings = old;
    return false;
  }
  return true;
}

bool driverSetChopper(int axis, ChopperMode mode, double hybridThresholdSps, const char** error) {
  if (!requireAxis(axis, error)) return false;
  if (mode == ChopperMode::Hybrid && !(hybridThresholdSps > 0.0)) {
    if (error) *error = "Hybrid mode needs hybrid_threshold_sps > 0";
    return false;
  }
  DriverSettings old = g_drivers[axis].settings;
  g_drivers[axis].settings.chopper = mode;
  g_drivers[axis].settings.hybridThresholdSps = mode == ChopperMode::Hybrid ? hybridThresholdSps : 0.0;
  if (!commitSettings(axis, error)) {
    g_drivers[axis].settings = old;
    return false;
  }
  return true;
}

bool driverSetCurrent(int axis, uint16_t runCurrentMa, float holdMultiplier, const char** error) {
  if (!requireAxis(axis, error)) return false;
  if (runCurrentMa == 0 || runCurrentMa > kMaxRunCurrentMa) {
    if (error) *error = "run_ma must be 1-1350";
    return false;
  }
  if (!(holdMultiplier >= 0.0f) || holdMultiplier > 1.0f) {
    if (error) *error = "hold_multiplier must be 0.0-1.0";
    return false;
  }
  DriverSettings old = g_drivers[axis].settings;
  g_drivers[axis].settings.runCurrentMa = runCurrentMa;
  g_drivers[axis].settings.holdMultiplier = holdMultiplier;
  if (!commitSettings(axis, error)) {
    g_drivers[axis].settings = old;
    return false;
  }
  return true;
}

bool driverSetInterpolation(int axis, bool on, const char** error) {
  if (!requireAxis(axis, error)) return false;
  DriverSettings old = g_drivers[axis].settings;
  g_drivers[axis].settings.interpolate = on;
  if (!commitSettings(axis, error)) {
    g_drivers[axis].settings = old;
    return false;
  }
  return true;
}

bool driverSetStallThreshold(int axis, uint8_t threshold, const char** error) {
  if (!requireAxis(axis, error)) return false;
  DriverSettings old = g_drivers[axis].settings;
  g_drivers[axis].settings.stallThreshold = threshold;
  if (!commitSettings(axis, error)) {
    g_drivers[axis].settings = old;
    return false;
  }
  return true;
}

bool driverSetInvertDirection(int axis, bool invert, const char** error) {
  if (!requireAxis(axis, error)) return false;
  DriverSettings old = g_drivers[axis].settings;
  g_drivers[axis].settings.invertDirection = invert;
  if (!commitSettings(axis, error)) {
    g_drivers[axis].settings = old;
    return false;
  }
  return true;
}

bool driverSetEnabled(int axis, bool on, const char** error) {
  if (!requireAxis(axis, error)) return false;
  DriverState& d = g_drivers[axis];
  if (on && !d.configured) {
#if !SELENO_DEVKIT
    if (error) *error = d.present ? "Driver not configured" : "Driver not responding on TMC UART";
    return false;
#endif
  }
  writeEnablePin(d, on);
  return true;
}

bool driverEnabled(int axis) {
  return validAxis(axis) && g_drivers[axis].enabled;
}

bool driverReinit(int axis) {
  if (!validAxis(axis)) return false;
  DriverState& d = g_drivers[axis];
  bool wasEnabled = d.enabled;
  // ENN high also clears a latched driver error (datasheet).
  writeEnablePin(d, false);
  bool ok = configure(d);
  if (wasEnabled && ok) {
    writeEnablePin(d, true);
  }
  return ok;
}

void driverStatusJson(int axis, JsonObject out) {
  if (!validAxis(axis)) {
    return;
  }
  DriverState& d = g_drivers[axis];
  const DriverSettings& s = d.settings;
  out["axis"] = axis;
  out["address"] = axis == 0 ? kTmcAddrMotor1 : kTmcAddrMotor2;
  out["present"] = d.present;
  out["configured"] = d.configured;
  out["enabled"] = d.enabled;
  out["diag"] = d.present ? (digitalRead(d.diagPin) == HIGH) : false;
  out["version"] = d.version;
  out["uart_errors"] = d.uartErrors;

  JsonObject cfg = out["settings"].to<JsonObject>();
  cfg["microsteps"] = s.microsteps;
  cfg["run_ma"] = s.runCurrentMa;
  cfg["hold_multiplier"] = s.holdMultiplier;
  cfg["chopper"] = chopperModeName(s.chopper);
  cfg["hybrid_threshold_sps"] = s.hybridThresholdSps;
  cfg["interpolate"] = s.interpolate;
  cfg["stall_threshold"] = s.stallThreshold;
  cfg["invert_direction"] = s.invertDirection;

  if (!d.present) {
    return;
  }
  JsonObject live = out["live"].to<JsonObject>();
  uint32_t v = 0;
  if (readChecked(d, REG_IFCNT, v)) live["ifcnt"] = v & 0xFF;
  if (readChecked(d, REG_GSTAT, v)) {
    live["gstat_reset"] = (v & GSTAT_RESET) != 0;
    live["gstat_drv_err"] = (v & GSTAT_DRV_ERR) != 0;
    live["gstat_uv_cp"] = (v & GSTAT_UV_CP) != 0;
  }
  if (readChecked(d, REG_CHOPCONF, v)) {
    live["microsteps"] = seleno::microstepsFromMres((v >> 24) & 0x0F);
    live["vsense"] = (v >> 17) & 1;
  }
  if (readChecked(d, REG_GCONF, v)) {
    live["gconf"] = v;
    live["spreadcycle"] = (v & GCONF_EN_SPREADCYCLE) != 0;
  }
  live["irun"] = d.tmc->irun();
  live["ihold"] = d.tmc->ihold();
  live["rms_current_ma"] = d.tmc->rms_current();
  if (readChecked(d, REG_TSTEP, v)) live["tstep"] = v;
  if (readChecked(d, REG_SG_RESULT, v)) live["sg_result"] = v & 0x3FF;
  if (readChecked(d, REG_MSCNT, v)) live["mscnt"] = v & 0x3FF;
  if (readChecked(d, REG_PWM_SCALE, v)) live["pwm_scale_sum"] = v & 0xFF;
  if (readChecked(d, REG_DRV_STATUS, v)) {
    addDrvStatusFlags(live["drv_status"].to<JsonObject>(), v);
  }
}

bool driverReadRegister(int axis, uint8_t reg, uint32_t& value) {
  if (!validAxis(axis)) return false;
  return readChecked(g_drivers[axis], reg & 0x7F, value);
}

bool driverWriteRegister(int axis, uint8_t reg, uint32_t value) {
  if (!validAxis(axis) || !g_drivers[axis].present) return false;
  g_drivers[axis].tmc->writeReg(reg & 0x7F, value);
  return true;
}

void driversPoll() {
  for (int i = 0; i < kAxisCount; i++) {
    pollDiag(i);
  }
  uint32_t now = millis();
  // One driver per half interval keeps each pass to a few milliseconds.
  if (now - g_lastPollMs < kDriverPollIntervalMs / kAxisCount) {
    return;
  }
  g_lastPollMs = now;
  pollRegisters(g_pollAxis);
  g_pollAxis = (g_pollAxis + 1) % kAxisCount;
}
