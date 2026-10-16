// Board tests and motor tools for the menu.
#include "ui_tests.h"

#include <cmath>

#include "config.h"
#include "display.h"
#include "estop.h"
#include "front_panel.h"
#include "host_link.h"
#include "i2c_bus.h"
#include "imu.h"
#include "motors.h"
#include "stepper.h"
#include "storage.h"
#include "tmc_drivers.h"
#include "ui.h"
#include "ui_dialogs.h"

namespace ui {

const float kJogSpeeds[kJogSpeedCount] = {200.0f, 800.0f, 3200.0f};
const char* const kJogSpeedNames[kJogSpeedCount] = {"Slow", "Medium", "Fast"};

namespace {

int g_axis = 0;
bool g_ledTest = false;

// One line of "label ........ value" in Font0.
int32_t infoLine(Gfx& g, int32_t y, const String& label, const String& value, uint16_t valueColor) {
  const Theme& t = theme();
  int32_t w = screenW();
  g.setFont(fontSmall());
  g.setTextDatum(textdatum_t::top_left);
  g.setTextColor(t.textDim);
  g.drawString(label, 4, y);
  g.setTextDatum(textdatum_t::top_right);
  g.setTextColor(valueColor);
  g.drawString(fitText(g, value, w - 8 - g.textWidth(label) - 6), w - 4, y);
  return y + 10;
}

const char* yesNo(bool v) { return v ? "yes" : "no"; }

String hex2(uint8_t v) {
  char buf[6];
  snprintf(buf, sizeof(buf), "0x%02X", v);
  return buf;
}

// ------------------------------------------------------------ jog

class JogScreen : public Screen {
 public:
  const char* name() const override { return "jog"; }
  uint32_t refreshMs() const override { return 100; }
  String footerHint() const override { return "hold\x11\x10 \x1e\x1f spd"; }

  void enter() override {
    axis_ = g_axis;
    savedSpeed_ = motorSpeedSps(axis_);
    dir_ = 0;
  }

  void leave() override {
    stopJog();
    motorSetSpeedSps(axis_, savedSpeed_, nullptr);
  }

  void stateJson(JsonObject out) const override {
    out["axis"] = axis_;
    out["dir"] = dir_;
    out["speed"] = kJogSpeeds[settings().jogSpeed];
    out["moving"] = stepperIsMoving(axis_);
    out["position"] = motorPosition(axis_);
  }

  void key(const KeyEvent& e) override {
    if (e.action != KeyAction::Press && e.action != KeyAction::Repeat) return;
    Settings& s = settings();
    switch (e.key) {
      case Key::Up:
        if (s.jogSpeed + 1 < kJogSpeedCount) s.jogSpeed++;
        changeSpeed();
        break;
      case Key::Down:
        if (s.jogSpeed > 0) s.jogSpeed--;
        changeSpeed();
        break;
      case Key::Select:
        motorSetPosition(axis_, 0);
        toast("Position set to zero", 1000);
        break;
      case Key::Return:
        if (e.action == KeyAction::Press) pop();
        break;
      default: break;  // LEFT/RIGHT work by holding (tick)
    }
  }

  void tick(uint32_t) override {
    int want = keyHeld(Key::Right) ? 1 : (keyHeld(Key::Left) ? -1 : 0);
    if (want == dir_) {
      if (dir_ != 0 && !stepperIsMoving(axis_)) dir_ = 0;  // refused or stopped elsewhere
      return;
    }
    if (dir_ != 0) {
      motorStop(axis_);
      dir_ = 0;
    }
    if (want != 0 && !stepperIsMoving(axis_)) {
      int axis = axis_;
      if (panelMove(axis, [axis, want](const char** error) {
            return motorSetSpeedSps(axis, kJogSpeeds[settings().jogSpeed], error) &&
                   motorStartContinuous(axis, want > 0, error);
          })) {
        dir_ = want;
      }
    }
  }

  void draw(Gfx& g) override {
    const Theme& t = theme();
    int32_t w = screenW();
    drawTitleBar(g, String("Jog ") + axisName(axis_), motorEngaged(axis_) ? "on" : "off");
    double rev = static_cast<double>(motorPosition(axis_)) / std::max<uint32_t>(1, motorStepsPerRev(axis_));
    g.setFont(fontHuge());
    g.setTextDatum(textdatum_t::top_center);
    g.setTextColor(t.text);
    char buf[24];
    snprintf(buf, sizeof(buf), "%.1f", rev * 360.0);
    g.drawString(buf, w / 2 - 4, kBarH + 8);
    g.drawCircle(w / 2 + g.textWidth(buf) / 2 - 1, kBarH + 11, 2, t.text);
    g.setFont(fontSmall());
    g.setTextColor(t.textDim);
    snprintf(buf, sizeof(buf), "%lld steps", static_cast<long long>(motorPosition(axis_)));
    g.drawString(buf, w / 2, kBarH + 30);
    g.drawString("motor shaft", w / 2, kBarH + 40);

    int32_t y = kBarH + 56;
    int32_t bw = (w - 16) / kJogSpeedCount;
    for (int i = 0; i < kJogSpeedCount; i++) {
      bool sel = i == settings().jogSpeed;
      int32_t x = 4 + i * (bw + 4);
      g.fillRoundRect(x, y, bw, 15, 3, sel ? t.accent : t.surface);
      g.setTextDatum(textdatum_t::middle_center);
      g.setTextColor(sel ? t.onAccent : t.textDim);
      g.setFont(fontBody());
      g.drawString(kJogSpeedNames[i], x + bw / 2, y + 7);
    }
    y += 22;
    g.setFont(fontSmall());
    g.setTextDatum(textdatum_t::top_left);
    g.setTextColor(t.textDim);
    bool moving = stepperIsMoving(axis_);
    snprintf(buf, sizeof(buf), "%s  1/%u", moving ? (dir_ > 0 ? "> forward" : (dir_ < 0 ? "< back" : "stopping"))
                                                 : "idle",
             static_cast<unsigned>(motorMicrosteps(axis_)));
    g.drawString(buf, 4, y);
    y += 11;
    drawIconText(g, "\x07 = set zero here", 4, y + 4, t.textDim);
    if (moving) {
      drawArrow(g, dir_ >= 0 ? w - 10 : 10, kBarH + 18, 6, dir_ >= 0 ? 'r' : 'l', t.accent);
    }
  }

 private:
  int axis_ = 0;
  int dir_ = 0;
  double savedSpeed_ = 0.0;

  void stopJog() {
    if (dir_ != 0 || (stepperIsMoving(axis_) && motorMotionSource(axis_) == MotionSource::Panel)) {
      motorStop(axis_);
    }
    dir_ = 0;
  }

  void changeSpeed() {
    saveSettings();
    if (dir_ != 0) {
      // Speeds up or slows down the running jog (ramped).
      motorSetSpeedSps(axis_, kJogSpeeds[settings().jogSpeed], nullptr);
    }
  }
};

JogScreen g_jog;

// ------------------------------------------------------------ driver

class DriverScreen : public Screen {
 public:
  const char* name() const override { return "driver"; }
  uint32_t refreshMs() const override { return 1000; }
  String footerHint() const override { return "\x11\x10 axis \x1e\x1f"; }

  void enter() override {
    axis_ = g_axis;
    scroll_ = 0;
    readNow();
  }

  void tick(uint32_t now) override {
    if (now - lastRead_ >= 1000) readNow();
  }

  void key(const KeyEvent& e) override {
    if (e.action == KeyAction::Long) return;
    switch (e.key) {
      case Key::Left:
      case Key::Right:
        axis_ = 1 - axis_;
        readNow();
        break;
      case Key::Down: scroll_ += 20; break;
      case Key::Up: scroll_ = std::max<int32_t>(0, scroll_ - 20); break;
      case Key::Select:
        driverReinit(axis_);
        readNow();
        toast("Driver set up again", 1000);
        break;
      case Key::Return: pop(); break;
      default: break;
    }
  }

  void draw(Gfx& g) override {
    const Theme& t = theme();
    drawTitleBar(g, String("Driver ") + axisName(axis_), axis_ == 0 ? "U7 @0" : "U8 @1");
    JsonObjectConst d = doc_.as<JsonObjectConst>();
    JsonObjectConst live = d["live"];
    JsonObjectConst cfg = d["settings"];
    JsonObjectConst flags = live["drv_status"];
    int32_t top = kBarH + 3;
    g.setClipRect(0, top, screenW(), screenH() - kFooterH - top);
    int32_t y = top - scroll_;
    bool present = d["present"] | false;
    y = infoLine(g, y, "UART", present ? "answers" : "no answer", present ? t.ok : t.bad);
    y = infoLine(g, y, "Version", hex2(d["version"] | 0), t.text);
    y = infoLine(g, y, "Enabled", yesNo(d["enabled"] | false), t.text);
    y = infoLine(g, y, "Microsteps", String(live["microsteps"] | 0) + (motorMicrostepsAuto(axis_) ? " auto" : ""), t.text);
    y = infoLine(g, y, "Chopper", String(cfg["chopper"] | "?") + ((live["spreadcycle"] | false) ? " (spread)" : ""),
                 t.text);
    y = infoLine(g, y, "Current", String(cfg["run_ma"] | 0) + " mA", t.text);
    y = infoLine(g, y, "Live RMS", String(live["rms_current_ma"] | 0) + " mA", t.text);
    bool ot = flags["ot"] | false;
    bool otpw = flags["otpw"] | false;
    y = infoLine(g, y, "Temp", ot ? "OVERHEAT" : (otpw ? "warning" : "ok"), ot ? t.bad : (otpw ? t.warn : t.ok));
    bool shorted = (flags["s2ga"] | false) || (flags["s2gb"] | false);
    y = infoLine(g, y, "Short", shorted ? "YES" : "no", shorted ? t.bad : t.ok);
    bool diag = d["diag"] | false;
    y = infoLine(g, y, "DIAG pin", diag ? "high" : "low", diag ? t.warn : t.text);
    y = infoLine(g, y, "Reset seen", yesNo(live["gstat_reset"] | false), t.text);
    y = infoLine(g, y, "IFCNT", String(live["ifcnt"] | 0), t.text);
    y = infoLine(g, y, "UART errors", String(d["uart_errors"] | 0), t.text);
    y = infoLine(g, y, "SG_RESULT", String(live["sg_result"] | 0), t.text);
    y = infoLine(g, y, "TSTEP", String(live["tstep"] | 0), t.text);
    y = infoLine(g, y, "Steps/rev", String(motorStepsPerRev(axis_)), t.text);
    g.setFont(fontSmall());
    g.setTextColor(t.textDim);
    g.setTextDatum(textdatum_t::top_left);
    drawIconText(g, "\x07 = set the driver up again", 4, y + 8, t.textDim);
    g.clearClipRect();
  }

 private:
  int axis_ = 0;
  int32_t scroll_ = 0;
  uint32_t lastRead_ = 0;
  JsonDocument doc_;

  void readNow() {
    doc_.clear();
    driverStatusJson(axis_, doc_.to<JsonObject>());
    lastRead_ = millis();
    invalidate();
  }
};

DriverScreen g_driver;

// ------------------------------------------------------------ self test

class SelfTestScreen : public Screen {
 public:
  const char* name() const override { return "self_test"; }
  String footerHint() const override { return "\x07 again"; }

  void enter() override { run(); }

  void stateJson(JsonObject out) const override {
    JsonArray a = out["checks"].to<JsonArray>();
    for (int i = 0; i < count_; i++) {
      JsonObject c = a.add<JsonObject>();
      c["name"] = checks_[i].name;
      c["result"] = checks_[i].level == 0 ? "ok" : (checks_[i].level == 1 ? "warn" : "fail");
      c["detail"] = checks_[i].detail;
    }
  }

  void key(const KeyEvent& e) override {
    if (e.action != KeyAction::Press) return;
    if (e.key == Key::Select) run();
    if (e.key == Key::Return) pop();
  }

  void draw(Gfx& g) override {
    const Theme& t = theme();
    int ok = 0;
    for (int i = 0; i < count_; i++) ok += checks_[i].level == 0;
    drawTitleBar(g, "Self test", String(ok) + "/" + String(count_));
    int32_t y = kBarH + 3;
    int32_t w = screenW();
    for (int i = 0; i < count_; i++) {
      const Check& c = checks_[i];
      uint16_t color = c.level == 0 ? t.ok : (c.level == 1 ? t.warn : t.bad);
      g.fillCircle(7, y + 5, 3, color);
      g.setFont(fontBody());
      g.setTextDatum(textdatum_t::top_left);
      g.setTextColor(t.text);
      g.drawString(c.name, 14, y);
      g.setFont(fontSmall());
      g.setTextDatum(textdatum_t::top_right);
      g.setTextColor(t.textDim);
      g.drawString(fitText(g, c.detail, w - 70), w - 3, y + 2);
      y += 12;
    }
  }

 private:
  struct Check {
    const char* name;
    uint8_t level;  // 0 ok, 1 warning, 2 fail
    String detail;
  };
  Check checks_[11];
  int count_ = 0;

  void add(const char* name, uint8_t level, const String& detail) {
    if (count_ < 11) checks_[count_++] = {name, level, detail};
  }

  void run() {
    count_ = 0;
    for (int axis = 0; axis < kAxisCount; axis++) {
      bool ok = driverPresent(axis);
      add(axis == 0 ? "RA driver" : "Dec driver", ok ? 0 : (SELENO_DEVKIT ? 1 : 2), ok ? "UART ok" : "no answer");
    }
    add("Buttons/LEDs", panelExpanderPresent() ? 0 : 2, panelExpanderPresent() ? "MCP23017" : "no 0x20");
    if (!imuEnabled()) {
      add("IMU", 1, "switched off");
    } else {
      add("IMU", imuPresent() ? 0 : 1, imuPresent() ? "BNO085" : "not found");
    }
    uint8_t found[16];
    size_t n = i2cScan(found, sizeof(found));
    add("I2C bus", n > 0 ? 0 : 2, String(n) + " device" + (n == 1 ? "" : "s"));
    add("Storage", storageMounted() ? 0 : 2, storageMounted() ? "mounted" : "failed");
    add("Display", displayInitialized() ? 0 : 2, displayInitialized() ? "ok" : "init failed");
    Link l = link();
    add("Pi link", l == Link::Connected ? 0 : (l == Link::Waiting ? 1 : 2), linkName(l));
    uint32_t heap = ESP.getFreeHeap();
    add("Memory", heap > 60000 ? 0 : 1, String(heap / 1024) + " KB free");
    add("E-stop", estopLatched() ? 1 : 0, estopLatched() ? "latched" : "clear");
    invalidate();
  }
};

SelfTestScreen g_selfTest;

// ------------------------------------------------------------ buttons

class ButtonTestScreen : public Screen {
 public:
  const char* name() const override { return "button_test"; }
  uint32_t refreshMs() const override { return 60; }
  String footerHint() const override { return "hold\x1b exit"; }

  void enter() override {
    for (uint32_t& c : counts_) c = 0;
  }

  void stateJson(JsonObject out) const override {
    JsonObject c = out["counts"].to<JsonObject>();
    for (int i = 0; i < static_cast<int>(Key::Count); i++) c[keyName(static_cast<Key>(i))] = counts_[i];
  }

  void key(const KeyEvent& e) override {
    if (e.key == Key::Return && e.action == KeyAction::Long) {
      pop();
      return;
    }
    if (e.action == KeyAction::Press) counts_[static_cast<int>(e.key)]++;
  }

  void draw(Gfx& g) override {
    const Theme& t = theme();
    int32_t w = screenW();
    drawTitleBar(g, "Buttons");
    int32_t cx = w / 2;
    int32_t cy = kBarH + 44;
    int32_t s = 13;
    pad(g, Key::Up, cx, cy - 2 * s, "UP");
    pad(g, Key::Down, cx, cy + 2 * s, "DN");
    pad(g, Key::Left, cx - 2 * s - 6, cy, "LT");
    pad(g, Key::Right, cx + 2 * s + 6, cy, "RT");
    pad(g, Key::Select, cx, cy, "OK");
    pad(g, Key::Return, w - 18, kBarH + 14, "RET");

    g.setFont(fontSmall());
    g.setTextDatum(textdatum_t::top_left);
    g.setTextColor(t.textDim);
    int32_t y = cy + 3 * s + 2;
    JsonDocument st;
    panelStatusJson(st.to<JsonObject>());
    char buf[40];
    snprintf(buf, sizeof(buf), "INT %lu  raw %04X", static_cast<unsigned long>(st["interrupts"] | 0u),
             static_cast<unsigned>(st["raw_gpio"] | 0u));
    g.drawString(buf, 4, y);
    g.drawString(String("expander ") + ((st["expander_present"] | false) ? "ok" : "MISSING"), 4, y + 10);
  }

 private:
  uint32_t counts_[static_cast<int>(Key::Count)] = {0};

  void pad(Gfx& g, Key k, int32_t cx, int32_t cy, const char* label) {
    const Theme& t = theme();
    bool down = keyHeld(k);
    g.fillRoundRect(cx - 12, cy - 11, 24, 22, 4, down ? t.accent : t.surface);
    g.setFont(fontSmall());
    g.setTextDatum(textdatum_t::middle_center);
    g.setTextColor(down ? t.onAccent : t.text);
    g.drawString(label, cx, cy - 4);
    g.setTextColor(down ? t.onAccent : t.textDim);
    g.drawString(String(counts_[static_cast<int>(k)]), cx, cy + 5);
  }
};

ButtonTestScreen g_buttons;

// ------------------------------------------------------------ LEDs

class LedTestScreen : public Screen {
 public:
  const char* name() const override { return "led_test"; }
  uint32_t refreshMs() const override { return chase_ ? 150 : 0; }
  String footerHint() const override { return "\x07 on/off \x10 chase"; }

  void enter() override {
    for (int i = 0; i < kLedCount; i++) saved_[i] = ledState(i);
    savedMuted_ = ledsMuted();
    ledsSetMuted(false);
    g_ledTest = true;
    chase_ = false;
    index_ = 0;
  }

  void leave() override {
    for (int i = 0; i < kLedCount; i++) ledSet(i, saved_[i]);
    ledsSetMuted(savedMuted_);
    g_ledTest = false;
  }

  void tick(uint32_t now) override {
    if (!chase_ || now - lastStep_ < 250) return;
    lastStep_ = now;
    step_ = (step_ + 1) % kLedCount;
    for (int i = 0; i < kLedCount; i++) ledSet(i, i == step_);
    invalidate();
  }

  void key(const KeyEvent& e) override {
    if (e.action == KeyAction::Long) return;
    switch (e.key) {
      case Key::Up: index_ = (index_ + kLedCount - 1) % kLedCount; break;
      case Key::Down: index_ = (index_ + 1) % kLedCount; break;
      case Key::Select:
        chase_ = false;
        ledToggle(index_);
        break;
      case Key::Right: chase_ = !chase_; break;
      case Key::Left:
        chase_ = false;
        {
          bool anyOn = false;
          for (int i = 0; i < kLedCount; i++) anyOn |= ledState(i);
          for (int i = 0; i < kLedCount; i++) ledSet(i, !anyOn);
        }
        break;
      case Key::Return: pop(); break;
      default: break;
    }
  }

  void draw(Gfx& g) override {
    const Theme& t = theme();
    int32_t w = screenW();
    drawTitleBar(g, "LEDs", chase_ ? "chase" : "");
    static const uint16_t kColors[kLedCount] = {rgb(80, 160, 255), rgb(255, 210, 0), rgb(40, 90, 255),
                                                rgb(240, 240, 240), rgb(40, 220, 80), rgb(255, 50, 40)};
    for (int i = 0; i < kLedCount; i++) {
      int32_t y = kBarH + 3 + i * 15;
      bool sel = i == index_;
      if (sel) {
        g.fillRect(0, y, w, 15, t.select);
        g.fillRect(0, y, 2, 15, t.accent);
      }
      bool on = ledState(i);
      g.fillCircle(12, y + 7, 4, on ? kColors[i] : t.surface);
      g.drawCircle(12, y + 7, 4, t.textDim);
      g.setFont(fontBody());
      g.setTextDatum(textdatum_t::middle_left);
      g.setTextColor(sel ? t.text : t.textDim);
      g.drawString(ledName(i), 22, y + 7);
      g.setTextDatum(textdatum_t::middle_right);
      g.drawString(on ? "on" : "off", w - 6, y + 7);
    }
    g.setFont(fontSmall());
    g.setTextDatum(textdatum_t::top_left);
    g.setTextColor(t.textDim);
    drawIconText(g, "\x11 all on/off", 4, kBarH + 3 + kLedCount * 15 + 8, t.textDim);
    g.drawString("(board = IO2)", 4, kBarH + 3 + kLedCount * 15 + 16);
  }

 private:
  bool saved_[kLedCount] = {false};
  bool savedMuted_ = false;
  bool chase_ = false;
  int index_ = 0;
  int step_ = 0;
  uint32_t lastStep_ = 0;
};

LedTestScreen g_leds;

// ------------------------------------------------------------ display

class DisplayTestScreen : public Screen {
 public:
  const char* name() const override { return "display_test"; }
  bool wantsFooter() const override { return false; }

  void enter() override { page_ = 0; }

  void stateJson(JsonObject out) const override {
    out["page"] = page_;
    out["page_name"] = kPages[page_];
  }

  void key(const KeyEvent& e) override {
    if (e.action == KeyAction::Long) return;
    switch (e.key) {
      case Key::Right:
      case Key::Down:
      case Key::Select: page_ = (page_ + 1) % kPageCount; break;
      case Key::Left:
      case Key::Up: page_ = (page_ + kPageCount - 1) % kPageCount; break;
      case Key::Return: pop(); break;
      default: break;
    }
  }

  void draw(Gfx& g) override {
    int32_t w = screenW();
    int32_t h = screenH();
    uint16_t label = 0xFFFF;
    switch (page_) {
      case 0: {
        static const uint16_t kBars[8] = {0xFFFF, rgb(255, 255, 0), rgb(0, 255, 255), rgb(0, 255, 0),
                                          rgb(255, 0, 255), rgb(255, 0, 0), rgb(0, 0, 255), 0x0000};
        for (int i = 0; i < 8; i++) g.fillRect(i * w / 8, 0, (i + 1) * w / 8 - i * w / 8, h, kBars[i]);
        break;
      }
      case 1:
        // Every edge pixel lit: a missing or doubled line means the panel
        // offsets (display.cpp kLcdOffset*) are wrong.
        g.fillScreen(0x0000);
        g.drawRect(0, 0, w, h, 0xFFFF);
        g.drawRect(2, 2, w - 4, h - 4, rgb(255, 0, 0));
        g.drawFastHLine(w / 2 - 6, h / 2, 13, 0xFFFF);
        g.drawFastVLine(w / 2, h / 2 - 6, 13, 0xFFFF);
        g.setFont(fontSmall());
        g.setTextColor(0xFFFF);
        g.setTextDatum(textdatum_t::top_left);
        g.drawString("TL", 5, 5);
        g.setTextDatum(textdatum_t::top_right);
        g.drawString("TR", w - 5, 5);
        g.setTextDatum(textdatum_t::bottom_left);
        g.drawString("BL", 5, h - 5);
        g.setTextDatum(textdatum_t::bottom_right);
        g.drawString("BR", w - 5, h - 5);
        break;
      case 2: {
        int32_t band = h / 4;
        for (int32_t x = 0; x < w; x++) {
          uint8_t v = static_cast<uint8_t>(x * 255 / (w - 1));
          g.drawFastVLine(x, 0, band, rgb(v, 0, 0));
          g.drawFastVLine(x, band, band, rgb(0, v, 0));
          g.drawFastVLine(x, 2 * band, band, rgb(0, 0, v));
          g.drawFastVLine(x, 3 * band, h - 3 * band, rgb(v, v, v));
        }
        break;
      }
      case 3: g.fillScreen(0xFFFF); label = 0x0000; break;
      case 4: g.fillScreen(rgb(255, 0, 0)); break;
      case 5: g.fillScreen(rgb(0, 255, 0)); label = 0x0000; break;
      case 6: g.fillScreen(rgb(0, 0, 255)); break;
      case 7: {
        g.fillScreen(0x0000);
        g.setTextColor(0xFFFF);
        g.setTextDatum(textdatum_t::top_left);
        int32_t y = 4;
        g.setFont(fontSmall());
        g.drawString("Font0 6x8 ABC abc 123", 3, y);
        y += 12;
        g.setFont(fontBody());
        g.drawString("DejaVu9 ABC abc", 3, y);
        y += 14;
        g.setFont(fontLarge());
        g.drawString("DejaVu12 Abc", 3, y);
        y += 18;
        g.setFont(fontHuge());
        g.drawString("Dv18 Ab", 3, y);
        y += 24;
        g.setFont(fontLogo());
        g.drawString("SELENO", 3, y);
        break;
      }
      default: break;
    }
    g.setFont(fontSmall());
    g.setTextDatum(textdatum_t::bottom_center);
    g.setTextColor(label);
    String caption = String(page_ + 1) + "/" + kPageCount + " " + kPages[page_];
    g.drawString(caption, w / 2, page_ == 1 ? h - 16 : h - 3);
  }

 private:
  static constexpr int kPageCount = 8;
  static constexpr const char* kPages[kPageCount] = {"colour bars", "edges", "gradients", "white",
                                                     "red",         "green", "blue",      "fonts"};
  int page_ = 0;
};

constexpr const char* DisplayTestScreen::kPages[];

DisplayTestScreen g_display;

// ------------------------------------------------------------ IMU

class ImuScreen : public Screen {
 public:
  const char* name() const override { return "imu"; }
  uint32_t refreshMs() const override { return 200; }
  String footerHint() const override { return "\x07 restart"; }

  void key(const KeyEvent& e) override {
    if (e.action != KeyAction::Press) return;
    if (e.key == Key::Select) {
      imuRequestReinit();
      toast("Looking for the IMU again", 1200);
    }
    if (e.key == Key::Return) pop();
  }

  void draw(Gfx& g) override {
    const Theme& t = theme();
    JsonDocument st;
    imuStatusJson(st.to<JsonObject>());
    JsonDocument rd;
    imuReadingJson(rd.to<JsonObject>());
    bool present = st["present"] | false;
    drawTitleBar(g, "IMU", present ? hex2(st["address"] | 0) : "");
    int32_t y = kBarH + 4;
    if (!present) {
      g.setFont(fontBody());
      g.setTextDatum(textdatum_t::top_left);
      g.setTextColor((st["enabled"] | true) ? t.warn : t.textDim);
      g.drawString((st["enabled"] | true) ? "No BNO085 found" : "IMU switched off", 4, y);
      y += 14;
      g.setFont(fontSmall());
      g.setTextColor(t.textDim);
      g.drawString("Check the J3 cable.", 4, y);
      g.drawString("Looking every 3 s.", 4, y + 10);
      y += 26;
    } else {
      JsonObjectConst rv = rd["rotation_vector"];
      JsonObjectConst e = rv["euler_deg"];
      y = angle(g, y, "Yaw", e["yaw"] | NAN);
      y = angle(g, y, "Pitch", e["pitch"] | NAN);
      y = angle(g, y, "Roll", e["roll"] | NAN);
      int acc = rv["accuracy"] | 0;
      static const char* const kAcc[4] = {"unreliable", "low", "medium", "high"};
      y = infoLine(g, y + 2, "Accuracy", kAcc[acc & 3], acc >= 2 ? t.ok : t.warn);
      JsonObjectConst gr = rd["gravity"];
      char buf[32];
      snprintf(buf, sizeof(buf), "%.1f %.1f %.1f", gr["x"] | 0.0f, gr["y"] | 0.0f, gr["z"] | 0.0f);
      y = infoLine(g, y, "Gravity", buf, t.text);
    }
    char buf[40];
    snprintf(buf, sizeof(buf), "%lu ev  %lu err  %lu rst", static_cast<unsigned long>(st["events"] | 0u),
             static_cast<unsigned long>(st["errors"] | 0u), static_cast<unsigned long>(st["resets"] | 0u));
    infoLine(g, y, "I2C", buf, t.textDim);
  }

 private:
  int32_t angle(Gfx& g, int32_t y, const char* label, float deg) {
    const Theme& t = theme();
    g.setFont(fontBody());
    g.setTextDatum(textdatum_t::top_left);
    g.setTextColor(t.textDim);
    g.drawString(label, 4, y + 3);
    g.setFont(fontLarge());
    g.setTextDatum(textdatum_t::top_right);
    g.setTextColor(t.text);
    char buf[16];
    if (std::isfinite(deg)) {
      snprintf(buf, sizeof(buf), "%.1f", deg);
    } else {
      snprintf(buf, sizeof(buf), "--");
    }
    g.drawString(buf, screenW() - 10, y);
    g.drawCircle(screenW() - 6, y + 3, 2, t.text);
    return y + 16;
  }
};

ImuScreen g_imu;

// ------------------------------------------------------------ I2C

class I2cScreen : public Screen {
 public:
  const char* name() const override { return "i2c"; }
  String footerHint() const override { return "\x07 scan"; }

  void enter() override { scan(); }

  void stateJson(JsonObject out) const override {
    JsonArray a = out["devices"].to<JsonArray>();
    for (size_t i = 0; i < count_; i++) a.add(found_[i]);
  }

  void key(const KeyEvent& e) override {
    if (e.action != KeyAction::Press) return;
    if (e.key == Key::Select) scan();
    if (e.key == Key::Return) pop();
  }

  void draw(Gfx& g) override {
    const Theme& t = theme();
    drawTitleBar(g, "I2C scan", String(count_) + " found");
    int32_t y = kBarH + 4;
    if (count_ == 0) {
      g.setFont(fontBody());
      g.setTextDatum(textdatum_t::top_left);
      g.setTextColor(t.bad);
      g.drawString("Nothing answered", 4, y);
      y += 14;
    }
    for (size_t i = 0; i < count_; i++) {
      y = infoLine(g, y, hex2(found_[i]), deviceName(found_[i]), t.text);
    }
    y += 4;
    y = infoLine(g, y, "Pins", "SDA 33  SCL 32", t.textDim);
    y = infoLine(g, y, "Speed", String(kI2cHz / 1000) + " kHz", t.textDim);
    infoLine(g, y, "Errors", String(i2cErrorCount()), t.textDim);
  }

 private:
  uint8_t found_[16];
  size_t count_ = 0;

  static String deviceName(uint8_t addr) {
    switch (addr) {
      case 0x20: return "MCP23017 (buttons)";
      case 0x4A:
      case 0x4B: return "BNO085 (IMU)";
      default: return "unknown";
    }
  }

  void scan() {
    count_ = i2cScan(found_, sizeof(found_));
    invalidate();
  }
};

I2cScreen g_i2c;

// ------------------------------------------------------------ Pi link

class LinkScreen : public Screen {
 public:
  const char* name() const override { return "link"; }
  uint32_t refreshMs() const override { return 500; }

  void key(const KeyEvent& e) override {
    if (e.action == KeyAction::Press && e.key == Key::Return) pop();
  }

  void draw(Gfx& g) override {
    const Theme& t = theme();
    Link l = link();
    drawTitleBar(g, "Pi link");
    int32_t y = kBarH + 4;
    y = infoLine(g, y, "State", linkName(l), l == Link::Connected ? t.ok : (l == Link::Waiting ? t.warn : t.bad));
    uint32_t rx = hostLastRxMs();
    y = infoLine(g, y, "Last byte", rx == 0 ? String("never") : String((millis() - rx) / 1000.0f, 1) + " s ago", t.text);
    String ports;
    for (int i = 0; i < hostPortCount(); i++) {
      if (i) ports += ", ";
      ports += hostPortName(i);
    }
    y = infoLine(g, y, "Ports", ports, t.text);
    y = infoLine(g, y, "Baud", String(kHostBaud), t.text);
    y = infoLine(g, y, "Dropped", String(hostDroppedEventCount()) + " events", t.text);
    y += 3;
    forEachHostStatus([&](const String& k, const String& v) {
      if (y < screenH() - kFooterH - 10) y = infoLine(g, y, k, v, t.textDim);
    });
  }
};

LinkScreen g_link;

}  // namespace

int& uiAxis() { return g_axis; }

const char* axisName(int axis) { return axis == 0 ? "RA" : "Dec"; }

bool ledTestActive() { return g_ledTest; }

bool panelMove(int axis, std::function<bool(const char**)> fn) {
  if (estopLatched()) {
    openConfirm("E-stop latched", "The emergency stop is on. Release it and move?",
                [axis, fn](bool yes) {
                  if (!yes) return;
                  estopClear();
                  panelMove(axis, fn);
                },
                "Release", "Cancel");
    return false;
  }
  const char* error = nullptr;
  if (!motorEngaged(axis) && !motorEngage(axis, true, &error)) {
    toast(String("Driver: ") + (error != nullptr ? error : "can't switch on"));
    return false;
  }
  PanelMotionScope scope;
  if (!fn(&error)) {
    toast(error != nullptr ? error : "Refused");
    return false;
  }
  return true;
}

Screen* jogScreen() { return &g_jog; }
Screen* driverScreen() { return &g_driver; }
Screen* selfTestScreen() { return &g_selfTest; }
Screen* buttonTestScreen() { return &g_buttons; }
Screen* ledTestScreen() { return &g_leds; }
Screen* displayTestScreen() { return &g_display; }
Screen* imuScreen() { return &g_imu; }
Screen* i2cScreen() { return &g_i2c; }
Screen* linkScreen() { return &g_link; }

}  // namespace ui

bool uiLedTestActive() {
  return ui::ledTestActive();
}
