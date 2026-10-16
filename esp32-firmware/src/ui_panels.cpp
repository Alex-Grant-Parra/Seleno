// The firmware's own menu panels. To add one, copy a block below: give it
// an id, a tab title and an order (tabs are sorted by order; the Pi's
// panels use 10 Scope, 20 Setup, 70 Pi), then add items with the builder
// (see ui_menu.h). Items read live state in their getter and act in their
// setter, so nothing needs updating by hand.
#include <esp_system.h>

#include "config.h"
#include "estop.h"
#include "front_panel.h"
#include "imu.h"
#include "motors.h"
#include "stepper.h"
#include "tmc_drivers.h"
#include "ui_dialogs.h"
#include "ui_menu.h"
#include "ui_tests.h"

namespace ui {

namespace {

constexpr uint16_t kMicrostepOptions[] = {0, 4, 8, 16, 32, 64, 128, 256};  // 0 = auto
constexpr int kMicrostepCount = sizeof(kMicrostepOptions) / sizeof(kMicrostepOptions[0]);
constexpr uint16_t kDimOptionsS[] = {0, 30, 60, 120, 300, 600};
constexpr int kDimCount = sizeof(kDimOptionsS) / sizeof(kDimOptionsS[0]);

void showError(const char* what, const char* error) {
  toast(String(what) + ": " + (error != nullptr ? error : "failed"));
}

String formatUptime(uint32_t ms) {
  uint32_t s = ms / 1000;
  char buf[24];
  if (s >= 86400) {
    snprintf(buf, sizeof(buf), "%ud %02uh %02um", static_cast<unsigned>(s / 86400),
             static_cast<unsigned>((s / 3600) % 24), static_cast<unsigned>((s / 60) % 60));
  } else {
    snprintf(buf, sizeof(buf), "%02u:%02u:%02u", static_cast<unsigned>(s / 3600),
             static_cast<unsigned>((s / 60) % 60), static_cast<unsigned>(s % 60));
  }
  return buf;
}

const char* resetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power on";
    case ESP_RST_EXT: return "reset pin";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "crash";
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT: return "watchdog";
    case ESP_RST_BROWNOUT: return "brown-out";
    default: return "other";
  }
}

// Turn the selected axis by whole motor revolutions at the jog speed.
void turnRevolutions(int revs) {
  int axis = uiAxis();
  panelMove(axis, [axis, revs](const char** error) {
    int64_t steps = static_cast<int64_t>(motorStepsPerRev(axis)) * revs;
    return motorSetSpeedSps(axis, kJogSpeeds[settings().jogSpeed], error) &&
           motorMoveSteps(axis, static_cast<int32_t>(steps), error);
  });
}

void buildMotors() {
  PanelBuilder p = addPanel("motors", "Motors", 30);
  p.choice("axis", "Axis", {"RA", "Dec"}, [] { return uiAxis(); }, [](int v) { uiAxis() = v; });
  p.toggle("engaged", "Driver on", [] { return motorEngaged(uiAxis()); },
           [](bool on) {
             const char* error = nullptr;
             if (!motorEngage(uiAxis(), on, &error)) showError("Driver", error);
           });
  p.screen("jog", "Jog motor", jogScreen());
  p.choice("jog_speed", "Jog speed", {"Slow", "Medium", "Fast"}, [] { return static_cast<int>(settings().jogSpeed); },
           [](int v) {
             settings().jogSpeed = static_cast<uint8_t>(v);
             saveSettings();
           });
  p.action("rev_fwd", "Turn +1 rev", [] { turnRevolutions(1); });
  p.action("rev_back", "Turn -1 rev", [] { turnRevolutions(-1); });
  p.info("position", "Position", [] {
    int axis = uiAxis();
    double deg = 360.0 * static_cast<double>(motorPosition(axis)) / std::max<uint32_t>(1, motorStepsPerRev(axis));
    return String(deg, 1) + " deg";
  });
  p.action("zero", "Set position to 0", [] {
    motorSetPosition(uiAxis(), 0);
    toast("Position is now 0", 1000);
  });
  p.choice("microsteps", "Microsteps", {"Auto", "1/4", "1/8", "1/16", "1/32", "1/64", "1/128", "1/256"},
           [] {
             int axis = uiAxis();
             if (motorMicrostepsAuto(axis)) return 0;
             for (int i = 1; i < kMicrostepCount; i++) {
               if (kMicrostepOptions[i] == motorMicrosteps(axis)) return i;
             }
             return 0;
           },
           [](int v) {
             const char* error = nullptr;
             if (!motorSetMicrosteps(uiAxis(), kMicrostepOptions[v], &error)) showError("Microsteps", error);
           });
  p.choice("chopper", "Chopper", {"Auto", "Silent", "SpreadCycle"},
           [] {
             switch (driverSettings(uiAxis()).chopper) {
               case ChopperMode::StealthChop: return 1;
               case ChopperMode::SpreadCycle: return 2;
               default: return 0;
             }
           },
           [](int v) {
             static const ChopperMode kModes[3] = {ChopperMode::Auto, ChopperMode::StealthChop,
                                                   ChopperMode::SpreadCycle};
             const char* error = nullptr;
             if (!driverSetChopper(uiAxis(), kModes[v], kAutoSpreadThresholdFsps, &error)) {
               showError("Chopper", error);
             }
           });
  p.number("current", "Current", 100, kMaxRunCurrentMa, 50, "mA",
           [] { return static_cast<int32_t>(driverSettings(uiAxis()).runCurrentMa); },
           [](int32_t v) {
             const char* error = nullptr;
             if (!driverSetCurrent(uiAxis(), static_cast<uint16_t>(v), driverSettings(uiAxis()).holdMultiplier,
                                   &error)) {
               showError("Current", error);
             }
           });
  p.screen("driver", "Driver status", driverScreen());
  p.action("stop_all", "Stop all motors", [] {
     motorsStopAll(MotionEnd::Stopped);
     toast("Stopping", 1000);
   }).hint = "\x07 stop";
  MenuItem& release = p.action("release_estop", "Release E-stop", [] {
    estopClear();
    toast("E-stop released", 1200);
  });
  release.refresh = [](MenuItem& i) { i.visible = estopLatched(); };
  release.visible = false;
  MenuItem& hold = p.action("release_hold", "Let the Pi move again", [] {
    panelStopAcknowledge();
    toast("The Pi can move the motors again", 1500);
  });
  hold.confirm = "The Pi hasn't confirmed the stop yet. Let it move the motors anyway?";
  hold.refresh = [](MenuItem& i) { i.visible = panelStopPending(); };
  hold.visible = false;
}

void buildTests() {
  PanelBuilder p = addPanel("test", "Test", 40);
  p.screen("self", "Self test", selfTestScreen());
  p.screen("buttons", "Buttons", buttonTestScreen());
  p.screen("leds", "LEDs", ledTestScreen());
  p.screen("display", "Display", displayTestScreen());
  p.screen("imu", "IMU", imuScreen());
  p.screen("i2c", "I2C scan", i2cScreen());
  p.screen("link", "Pi link", linkScreen());
  p.toggle("imu_on", "IMU polling", [] { return imuEnabled(); }, [](bool on) { imuSetEnabled(on); });
}

void buildDisplay() {
  PanelBuilder p = addPanel("display", "Display", 60);
  p.number("brightness", "Brightness", 5, 100, 5, "%", [] { return static_cast<int32_t>(settings().brightness); },
           [](int32_t v) {
             settings().brightness = static_cast<uint8_t>(v);
             applySettings();
             saveSettings();
           });
  p.choice("theme", "Theme", {"Dark", "Night (red)"}, [] { return static_cast<int>(settings().theme); },
           [](int v) {
             settings().theme = static_cast<uint8_t>(v);
             applySettings();
             saveSettings();
           });
  p.choice("rotation", "Rotation", {"Portrait", "Landscape", "Portrait 180", "Landscape 180"},
           [] { return static_cast<int>(settings().rotation); },
           [](int v) {
             settings().rotation = static_cast<uint8_t>(v);
             applySettings();
             saveSettings();
           });
  p.choice("dim", "Dim after", {"Never", "30 s", "1 min", "2 min", "5 min", "10 min"},
           [] {
             for (int i = 0; i < kDimCount; i++) {
               if (kDimOptionsS[i] == settings().dimAfterS) return i;
             }
             return 3;
           },
           [](int v) {
             settings().dimAfterS = kDimOptionsS[v];
             saveSettings();
           });
  p.toggle("leds", "Panel LEDs", [] { return !settings().ledsMuted; },
           [](bool on) {
             settings().ledsMuted = !on;
             applySettings();
             saveSettings();
           });
}

void buildAbout() {
  PanelBuilder p = addPanel("about", "About", 90);
  p.info("name", "Name", [] { return deviceName().length() > 0 ? deviceName() : String("-"); });
  p.info("firmware", "Firmware", [] { return String(SELENO_FW_VERSION) + (SELENO_DEVKIT ? " devkit" : ""); });
  p.info("board", "Board", [] { return String(SELENO_BOARD_NAME); });
  p.info("uptime", "Uptime", [] { return formatUptime(millis()); });
  p.info("pi", "Pi link", [] { return String(linkName(link())); });
  p.info("memory", "Free memory", [] { return String(ESP.getFreeHeap() / 1024) + " KB"; });
  p.info("reset", "Last reset", [] { return String(resetReason()); });
  p.action("restart", "Restart controller", [] {
     motorsStopAll(MotionEnd::Stopped);
     delay(300);
     ESP.restart();
   }).confirm = "Restart the ESP32? The motors stop and the Pi reconnects.";
}

}  // namespace

void buildLocalPanels() {
  buildMotors();
  buildTests();
  buildDisplay();
  buildAbout();
}

}  // namespace ui
