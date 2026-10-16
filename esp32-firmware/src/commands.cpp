#include "commands.h"

#include <Arduino.h>
#include <esp_system.h>

#include "config.h"
#include "display.h"
#include "estop.h"
#include "front_panel.h"
#include "host_link.h"
#include "i2c_bus.h"
#include "imu.h"
#include "logic/led_logic.h"
#include "motors.h"
#include "storage.h"
#include "tmc_drivers.h"

namespace {

const char* resetReasonName(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON: return "poweron";
    case ESP_RST_EXT: return "external";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT: return "wdt";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    default: return "unknown";
  }
}

void addFirmwareInfo(JsonObject data) {
  data["fw"] = SELENO_FW_VERSION;
  data["board"] = SELENO_BOARD_NAME;
  data["devkit"] = SELENO_DEVKIT != 0;
  data["uptime_ms"] = millis();
  data["port"] = hostPortName(hostCurrentRequestId().port);
}

void handlePing() {
  JsonDocument resp;
  addFirmwareInfo(resp["data"].to<JsonObject>());
  hostSendResponse(resp);
}

void handleInfo() {
  JsonDocument resp;
  JsonObject data = resp["data"].to<JsonObject>();
  addFirmwareInfo(data);
  data["build"] = __DATE__ " " __TIME__;
  data["reset_reason"] = resetReasonName(esp_reset_reason());
  data["heap_free"] = ESP.getFreeHeap();
  data["heap_min_free"] = ESP.getMinFreeHeap();
  data["dropped_events"] = hostDroppedEventCount();
  data["i2c_errors"] = i2cErrorCount();
  JsonArray ports = data["ports"].to<JsonArray>();
  for (int i = 0; i < hostPortCount(); i++) {
    ports.add(hostPortName(i));
  }
  JsonObject estop = data["estop"].to<JsonObject>();
  estop["active"] = estopLatched();
  estop["source"] = estopSource();
  JsonArray motors = data["motors"].to<JsonArray>();
  for (int i = 0; i < kAxisCount; i++) {
    JsonObject m = motors.add<JsonObject>();
    m["axis"] = i;
    m["motor"] = motorIdForAxis(i) != nullptr ? motorIdForAxis(i) : "";
    m["driver_present"] = driverPresent(i);
    m["enabled"] = motorEngaged(i);
    m["microsteps"] = motorMicrosteps(i);
    m["microstep_mode"] = motorMicrostepsAuto(i) ? "auto" : "fixed";
    m["steps_per_rev"] = motorStepsPerRev(i);
  }
  data["expander_present"] = panelExpanderPresent();
  data["imu_present"] = imuPresent();
  data["display_initialized"] = displayInitialized();
  data["storage_mounted"] = storageMounted();
  hostSendResponse(resp);
}

void handleReboot() {
  hostSendOk();
  hostOut().flush();
  delay(50);
  ESP.restart();
}

void handleEstop(JsonDocument& req) {
  const char* action = req["action"] | "status";
  if (strcmp(action, "trigger") == 0) {
    estopTrigger("host");
  } else if (strcmp(action, "clear") == 0) {
    estopClear();
  } else if (strcmp(action, "status") != 0) {
    hostSendError("Unknown estop action");
    return;
  }
  JsonDocument resp;
  JsonObject data = resp["data"].to<JsonObject>();
  data["active"] = estopLatched();
  data["source"] = estopSource();
  hostSendResponse(resp);
}

void handleStopAll() {
  motorsStopAll(MotionEnd::Stopped);
  hostSendOk();
}

void handleI2cScan() {
  uint8_t found[32];
  size_t n = i2cScan(found, sizeof(found));
  JsonDocument resp;
  JsonObject data = resp["data"].to<JsonObject>();
  JsonArray devices = data["devices"].to<JsonArray>();
  for (size_t i = 0; i < n; i++) {
    devices.add(found[i]);
  }
  hostSendResponse(resp);
}

void handlePanel(JsonDocument& req) {
  const char* action = req["action"] | "status";
  if (strcmp(action, "reinit") == 0) {
    panelReinit();
  } else if (strcmp(action, "status") != 0) {
    hostSendError("Unknown panel action");
    return;
  }
  JsonDocument resp;
  panelStatusJson(resp["data"].to<JsonObject>());
  hostSendResponse(resp);
}

void handleImu(JsonDocument& req) {
  const char* action = req["action"] | "status";
  JsonDocument resp;
  JsonObject data = resp["data"].to<JsonObject>();
  if (strcmp(action, "read") == 0) {
    if (!imuPresent()) {
      hostSendError("IMU not present");
      return;
    }
    imuReadingJson(data);
    hostSendResponse(resp);
    return;
  }
  if (strcmp(action, "enable") == 0) {
    imuSetEnabled(true);
  } else if (strcmp(action, "disable") == 0) {
    imuSetEnabled(false);
  } else if (strcmp(action, "reinit") == 0) {
    imuRequestReinit();
  } else if (strcmp(action, "status") != 0) {
    hostSendError("Unknown imu action");
    return;
  }
  imuStatusJson(data);
  hostSendResponse(resp);
}

void handleHost(JsonDocument& req) {
  const char* action = req["action"] | "status";
  uint16_t chunk = 0;
  uint16_t gap = 0;
  hostTxPacing(chunk, gap);
  if (strcmp(action, "set") == 0) {
    if (!req["tx_chunk"].isNull()) chunk = req["tx_chunk"].as<uint16_t>();
    if (!req["tx_gap_us"].isNull()) gap = req["tx_gap_us"].as<uint16_t>();
    if (gap > 20000) {
      hostSendError("tx_gap_us must be 0-20000");
      return;
    }
    hostSetTxPacing(chunk, gap);
  } else if (strcmp(action, "status") != 0) {
    hostSendError("Unknown host action");
    return;
  }
  JsonDocument resp;
  JsonObject data = resp["data"].to<JsonObject>();
  data["baud"] = kHostBaud;
  data["tx_chunk"] = chunk;
  data["tx_gap_us"] = gap;
  data["port"] = hostPortName(hostCurrentRequestId().port);
  JsonArray ports = data["ports"].to<JsonArray>();
  for (int i = 0; i < hostPortCount(); i++) {
    ports.add(hostPortName(i));
  }
  hostSendResponse(resp);
}

void handleLed(JsonDocument& req) {
  const char* name = req["led"] | "board";
  int led = ledFind(name);
  if (led < 0) {
    hostSendError("Unknown LED");
    return;
  }
  if (!req["mode"].isNull()) {
    const char* mode = req["mode"] | "";
    if (strcmp(mode, "on") == 0) {
      ledSet(led, true);
    } else if (strcmp(mode, "off") == 0) {
      ledSet(led, false);
    } else if (strcmp(mode, "blink") == 0) {
      ledBlink(led, req["interval_ms"] | seleno::kDefaultLedBlinkMs, req["auto_off_ms"] | 0u);
    } else if (strcmp(mode, "toggle") == 0) {
      ledToggle(led);
    } else {
      hostSendError("Invalid LED mode");
      return;
    }
    hostSendOk();
    return;
  }
  if (!req["on"].isNull()) {
    ledSet(led, req["on"].as<bool>());
    hostSendOk();
    return;
  }
  hostSendError("Missing LED mode");
}

}  // namespace

void handleCommandLine(char* line, size_t len) {
  JsonDocument req;
  DeserializationError err = deserializeJson(req, line, len);
  if (err) {
    hostSendError("Invalid JSON");
    return;
  }
  const char* cmd = req["cmd"] | "";
  if (cmd[0] == '\0') {
    hostSendError("Missing cmd");
    return;
  }

  if (strcmp(cmd, "ping") == 0) return handlePing();
  if (strcmp(cmd, "info") == 0) return handleInfo();
  if (strcmp(cmd, "reboot") == 0) return handleReboot();
  if (strcmp(cmd, "estop") == 0) return handleEstop(req);
  if (strcmp(cmd, "stop_all") == 0) return handleStopAll();
  if (strcmp(cmd, "i2c_scan") == 0) return handleI2cScan();
  if (strcmp(cmd, "panel") == 0 || strcmp(cmd, "buttons") == 0) return handlePanel(req);
  if (strcmp(cmd, "imu") == 0) return handleImu(req);
  if (strcmp(cmd, "led") == 0) return handleLed(req);
  if (strcmp(cmd, "host") == 0) return handleHost(req);

  if (handleDisplayCommand(cmd, req)) return;
  if (handleMotorCommand(cmd, req)) return;

  hostSendError("Unknown cmd");
}
