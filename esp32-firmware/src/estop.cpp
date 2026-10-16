#include "estop.h"

#include <Arduino.h>
#include <ArduinoJson.h>

#include "config.h"
#include "host_link.h"
#include "motors.h"

namespace {
volatile bool g_latched = false;
const char* volatile g_source = "";
portMUX_TYPE g_mux = portMUX_INITIALIZER_UNLOCKED;

volatile bool g_panelStopPending = false;
volatile uint32_t g_panelStopCount = 0;
uint32_t g_lastReturnMs = 0;  // 0 = no press waiting for its pair
volatile bool g_returnNavigates = false;
}  // namespace

void estopTrigger(const char* source) {
  // Stop first; bookkeeping and reporting can wait a few microseconds.
  motorsStopAll(MotionEnd::EmergencyStop);

  bool wasLatched;
  portENTER_CRITICAL_SAFE(&g_mux);
  wasLatched = g_latched;
  g_latched = true;
  if (!wasLatched) {
    g_source = source != nullptr ? source : "";
  }
  portEXIT_CRITICAL_SAFE(&g_mux);

  JsonDocument ev;
  ev["event"] = "estop";
  ev["active"] = true;
  ev["source"] = source != nullptr ? source : "";
  ev["already_latched"] = wasLatched;
  hostEmitEvent(ev);
}

void estopClear() {
  bool wasLatched;
  portENTER_CRITICAL_SAFE(&g_mux);
  wasLatched = g_latched;
  g_latched = false;
  g_source = "";
  portEXIT_CRITICAL_SAFE(&g_mux);

  if (wasLatched) {
    JsonDocument ev;
    ev["event"] = "estop";
    ev["active"] = false;
    hostEmitEvent(ev);
  }
}

bool estopLatched() {
  return g_latched;
}

const char* estopSource() {
  return g_source;
}

void panelStopTrigger(const char* reason) {
  motorsStopAll(MotionEnd::PanelStop);
  motorsSetHostHold(true);

  bool wasPending;
  portENTER_CRITICAL_SAFE(&g_mux);
  wasPending = g_panelStopPending;
  g_panelStopPending = true;
  g_panelStopCount = g_panelStopCount + 1;
  portEXIT_CRITICAL_SAFE(&g_mux);

  JsonDocument ev;
  ev["event"] = "ui";
  ev["type"] = "stop";
  ev["reason"] = reason != nullptr ? reason : "";
  ev["already_pending"] = wasPending;
  hostEmitEvent(ev);
}

bool panelStopPending() {
  return g_panelStopPending;
}

void panelStopAcknowledge() {
  portENTER_CRITICAL_SAFE(&g_mux);
  g_panelStopPending = false;
  portEXIT_CRITICAL_SAFE(&g_mux);
  motorsSetHostHold(false);
}

uint32_t panelStopCount() {
  return g_panelStopCount;
}

void returnButtonPressed(uint32_t nowMs) {
  bool hostMoving = false;
  double fsps = motorsHostMotionFsps(&hostMoving);

  portENTER_CRITICAL_SAFE(&g_mux);
  bool isDouble = g_lastReturnMs != 0 && nowMs - g_lastReturnMs <= kReturnDoublePressMs;
  // A pair is used up; the next press starts a new one.
  g_lastReturnMs = isDouble ? 0 : (nowMs == 0 ? 1 : nowMs);
  portEXIT_CRITICAL_SAFE(&g_mux);

  if (fsps > kReturnEstopAboveFsps) {
    estopTrigger("button");
    return;
  }
  if (isDouble && hostMoving && !g_returnNavigates) {
    panelStopTrigger("return_double");
  }
}

void returnButtonSetNavigates(bool navigates) {
  g_returnNavigates = navigates;
}

bool returnButtonNavigates() {
  return g_returnNavigates;
}
