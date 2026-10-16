#include "estop.h"

#include <Arduino.h>
#include <ArduinoJson.h>

#include "host_link.h"
#include "motors.h"

namespace {
volatile bool g_latched = false;
const char* volatile g_source = "";
portMUX_TYPE g_mux = portMUX_INITIALIZER_UNLOCKED;
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
