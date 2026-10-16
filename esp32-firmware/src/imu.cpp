#include "imu.h"

#include <Adafruit_BNO08x.h>
#include <Arduino.h>
#include <Wire.h>
#include <cmath>

#include "board_pins.h"
#include "config.h"
#include "host_link.h"
#include "i2c_bus.h"

namespace {

constexpr uint32_t kProbeIntervalMs = 3000;
// No report at all for this long while reports are enabled counts as an error.
constexpr uint32_t kSilenceErrorMs = 1000;
constexpr int kMaxEventsPerPoll = 12;

struct Vec3 {
  float x = 0, y = 0, z = 0;
  uint32_t atMs = 0;
  uint8_t accuracy = 0;  // 0 unreliable ... 3 high
};

struct Quat {
  float i = 0, j = 0, k = 0, real = 1;
  float accuracyRad = 0;
  uint32_t atMs = 0;
  uint8_t accuracy = 0;
};

struct ImuData {
  Quat rotation;      // absolute (uses the magnetometer)
  Quat gameRotation;  // relative, no magnetometer, no jumps
  Vec3 gravity;       // m/s^2
  Vec3 magnetic;      // uT
};

Adafruit_BNO08x g_bno(-1);  // no reset line on J3
TaskHandle_t g_task = nullptr;
portMUX_TYPE g_mux = portMUX_INITIALIZER_UNLOCKED;
ImuData g_data;

volatile bool g_enabled = true;
volatile bool g_present = false;
volatile bool g_reinitRequested = false;
volatile uint8_t g_address = 0;
volatile uint32_t g_resets = 0;
volatile uint32_t g_errors = 0;
volatile uint32_t g_events = 0;
uint32_t g_lastEventMs = 0;
uint8_t g_consecutiveErrors = 0;
char g_firmware[24] = {0};
bool g_sh2Open = false;

void emitImuEvent(const char* name) {
  JsonDocument ev;
  ev["event"] = name;
  ev["device"] = "bno085";
  ev["address"] = g_address;
  hostEmitEvent(ev);
}

bool enableReports() {
  return g_bno.enableReport(SH2_ROTATION_VECTOR, kImuReportIntervalUs) &&
         g_bno.enableReport(SH2_GAME_ROTATION_VECTOR, kImuReportIntervalUs) &&
         g_bno.enableReport(SH2_GRAVITY, kImuReportIntervalUs) &&
         g_bno.enableReport(SH2_MAGNETIC_FIELD_CALIBRATED, 100000);  // 10 Hz
}

bool tryStart() {
  uint8_t address = 0;
  // Probe first: the library prints to Serial when its own probe fails,
  // which would land in the JSON stream.
  if (i2cProbe(BNO08X_ADDR)) {
    address = BNO08X_ADDR;
  } else if (i2cProbe(BNO08X_ADDR_ALT)) {
    address = BNO08X_ADDR_ALT;
  } else {
    return false;
  }
  // The SH2 layer has one global session; opening again without closing
  // leaks it and every later open fails. begin_I2C opens it as soon as the
  // address answers, even if a later step fails.
  if (g_sh2Open) {
    sh2_close();
    g_sh2Open = false;
  }
  bool started = g_bno.begin_I2C(address, &Wire);
  g_sh2Open = started || i2cProbe(address);
  if (!started) {
    return false;
  }
  if (!enableReports()) {
    return false;
  }
  g_address = address;
  const sh2_ProductId_t& id = g_bno.prodIds.entry[0];
  snprintf(g_firmware, sizeof(g_firmware), "%u.%u.%u", id.swVersionMajor, id.swVersionMinor,
           id.swVersionPatch);
  g_consecutiveErrors = 0;
  g_lastEventMs = millis();
  return true;
}

void storeQuat(Quat& q, float i, float j, float k, float real, float acc, uint8_t status, uint32_t now) {
  q.i = i;
  q.j = j;
  q.k = k;
  q.real = real;
  q.accuracyRad = acc;
  q.accuracy = status & 0x03;
  q.atMs = now;
}

void storeVec(Vec3& v, float x, float y, float z, uint8_t status, uint32_t now) {
  v.x = x;
  v.y = y;
  v.z = z;
  v.accuracy = status & 0x03;
  v.atMs = now;
}

void poll() {
  sh2_SensorValue_t value;
  int n = 0;
  uint32_t now = millis();
  while (n < kMaxEventsPerPoll && g_bno.getSensorEvent(&value)) {
    n++;
    portENTER_CRITICAL(&g_mux);
    switch (value.sensorId) {
      case SH2_ROTATION_VECTOR: {
        const auto& r = value.un.rotationVector;
        storeQuat(g_data.rotation, r.i, r.j, r.k, r.real, r.accuracy, value.status, now);
        break;
      }
      case SH2_GAME_ROTATION_VECTOR: {
        const auto& r = value.un.gameRotationVector;
        storeQuat(g_data.gameRotation, r.i, r.j, r.k, r.real, 0.0f, value.status, now);
        break;
      }
      case SH2_GRAVITY: {
        const auto& g = value.un.gravity;
        storeVec(g_data.gravity, g.x, g.y, g.z, value.status, now);
        break;
      }
      case SH2_MAGNETIC_FIELD_CALIBRATED: {
        const auto& m = value.un.magneticField;
        storeVec(g_data.magnetic, m.x, m.y, m.z, value.status, now);
        break;
      }
      default:
        break;
    }
    portEXIT_CRITICAL(&g_mux);
  }

  if (n > 0) {
    g_events += n;
    g_lastEventMs = now;
    g_consecutiveErrors = 0;
  } else if (now - g_lastEventMs > kSilenceErrorMs) {
    g_errors++;
    g_lastEventMs = now;
    g_consecutiveErrors++;
  }

  if (g_bno.wasReset()) {
    // The sensor hub restarted (brown-out, watchdog): reports are off again.
    g_resets++;
    enableReports();
    emitImuEvent("imu_reset");
  }
}

void imuTask(void*) {
  uint32_t lastProbe = 0;
  for (;;) {
    uint32_t now = millis();

    if (g_reinitRequested) {
      g_reinitRequested = false;
      g_present = false;
      lastProbe = 0;
    }

    if (!g_enabled) {
      ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(500));
      continue;
    }

    if (!g_present) {
      if (lastProbe == 0 || now - lastProbe >= kProbeIntervalMs) {
        lastProbe = now;
        if (tryStart()) {
          g_present = true;
          emitImuEvent("imu_found");
        }
      }
      ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(100));
      continue;
    }

    poll();
    if (g_consecutiveErrors >= kImuMaxConsecutiveErrors / 4) {
      // A few silent seconds in a row: the board was unplugged or the
      // sensor hub is wedged. Start over from detection.
      g_present = false;
      lastProbe = millis();
      emitImuEvent("imu_lost");
    }
    vTaskDelay(pdMS_TO_TICKS(kImuPollIntervalMs));
  }
}

// Yaw/pitch/roll in degrees from a unit quaternion (ZYX convention).
void addEuler(JsonObject out, const Quat& q) {
  float sinrCosp = 2.0f * (q.real * q.i + q.j * q.k);
  float cosrCosp = 1.0f - 2.0f * (q.i * q.i + q.j * q.j);
  float roll = atan2f(sinrCosp, cosrCosp);
  float sinp = 2.0f * (q.real * q.j - q.k * q.i);
  float pitch = fabsf(sinp) >= 1.0f ? copysignf(static_cast<float>(M_PI) / 2.0f, sinp) : asinf(sinp);
  float sinyCosp = 2.0f * (q.real * q.k + q.i * q.j);
  float cosyCosp = 1.0f - 2.0f * (q.j * q.j + q.k * q.k);
  float yaw = atan2f(sinyCosp, cosyCosp);
  constexpr float kDeg = 180.0f / static_cast<float>(M_PI);
  out["yaw"] = yaw * kDeg;
  out["pitch"] = pitch * kDeg;
  out["roll"] = roll * kDeg;
}

void addQuat(JsonObject parent, const char* name, const Quat& q, uint32_t now, bool withAccuracyRad) {
  if (q.atMs == 0) {
    return;
  }
  JsonObject o = parent[name].to<JsonObject>();
  o["i"] = q.i;
  o["j"] = q.j;
  o["k"] = q.k;
  o["real"] = q.real;
  if (withAccuracyRad) {
    o["accuracy_rad"] = q.accuracyRad;
  }
  o["accuracy"] = q.accuracy;
  o["age_ms"] = now - q.atMs;
  addEuler(o["euler_deg"].to<JsonObject>(), q);
}

void addVec(JsonObject parent, const char* name, const Vec3& v, uint32_t now) {
  if (v.atMs == 0) {
    return;
  }
  JsonObject o = parent[name].to<JsonObject>();
  o["x"] = v.x;
  o["y"] = v.y;
  o["z"] = v.z;
  o["accuracy"] = v.accuracy;
  o["age_ms"] = now - v.atMs;
}

}  // namespace

void imuBegin() {
  // Core 0, below the panel task.
  xTaskCreatePinnedToCore(imuTask, "imu", 6144, nullptr, 2, &g_task, 0);
}

bool imuPresent() {
  return g_present;
}

void imuSetEnabled(bool on) {
  g_enabled = on;
  if (!on) {
    g_present = false;
  }
  if (g_task != nullptr) {
    xTaskNotifyGive(g_task);
  }
}

bool imuEnabled() {
  return g_enabled;
}

void imuRequestReinit() {
  g_reinitRequested = true;
  if (g_task != nullptr) {
    xTaskNotifyGive(g_task);
  }
}

void imuStatusJson(JsonObject out) {
  out["enabled"] = g_enabled;
  out["present"] = g_present;
  out["address"] = g_address;
  out["firmware"] = g_firmware;
  out["events"] = g_events;
  out["errors"] = g_errors;
  out["resets"] = g_resets;
  out["report_interval_us"] = kImuReportIntervalUs;
}

void imuReadingJson(JsonObject out) {
  ImuData d;
  portENTER_CRITICAL(&g_mux);
  d = g_data;
  portEXIT_CRITICAL(&g_mux);
  uint32_t now = millis();
  out["present"] = g_present;
  addQuat(out, "rotation_vector", d.rotation, now, true);
  addQuat(out, "game_rotation_vector", d.gameRotation, now, false);
  addVec(out, "gravity", d.gravity, now);
  addVec(out, "magnetic_field", d.magnetic, now);
}
