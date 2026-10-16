// Shared pieces of the front-panel UI (ui*.cpp). Everything here runs in
// the main loop task. See ui.h for the outside view and ui_menu.h for how
// to add panels.
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

#include <functional>

#include "display_device.h"

namespace ui {

using Gfx = LGFX_Sprite;

// ---------------------------------------------------------------- input

enum class Key : uint8_t { Select, Return, Up, Down, Left, Right, Count };

enum class KeyAction : uint8_t {
  Press,   // directions and RETURN: on press; SELECT: on a short release
  Repeat,  // a direction held down
  Long,    // SELECT or RETURN held for kLongPressMs (no Press follows)
};

struct KeyEvent {
  Key key;
  KeyAction action;
  bool doublePress;  // RETURN: the second press within kReturnDoublePressMs
  uint32_t atMs;
};

const char* keyName(Key key);
// Whether a key is down right now (for hold-to-move screens).
bool keyHeld(Key key);
bool parseKey(const char* name, Key& out);

constexpr uint32_t kRepeatDelayMs = 380;
constexpr uint32_t kRepeatIntervalMs = 90;
constexpr uint32_t kLongPressMs = 700;

// --------------------------------------------------------------- screens

// One full-screen view. Screens are static objects; the UI keeps a small
// stack of them (menu -> keyboard -> confirm, ...).
class Screen {
 public:
  virtual ~Screen() = default;
  virtual const char* name() const = 0;
  virtual void enter() {}
  virtual void leave() {}
  virtual void key(const KeyEvent& e) = 0;
  // Every uiPoll() while on top (also while the host draws).
  virtual void tick(uint32_t nowMs) { (void)nowMs; }
  virtual void draw(Gfx& g) = 0;
  // Redraw at least this often while on top (0 = only when invalidated).
  virtual uint32_t refreshMs() const { return 0; }
  // False where RETURN isn't a back key (tracking, loading): there a double
  // RETURN press stops host tracking (see estop.h).
  virtual bool returnNavigates() const { return true; }
  // Draw the status footer under this screen.
  virtual bool wantsFooter() const { return true; }
  // Short key help for the footer ("" = none).
  virtual String footerHint() const { return ""; }
  // Extra state for {"cmd":"ui","action":"state"} (tests, debugging).
  virtual void stateJson(JsonObject out) const { (void)out; }
};

void push(Screen* s);
void pop();
// Pop screens above s (s stays). No-op if s isn't on the stack.
void popTo(Screen* s);
// Clear the stack and show s.
void setRoot(Screen* s);
Screen* top();
bool onStack(const Screen* s);
void invalidate();
// A short message over whatever is shown.
void toast(const String& text, uint32_t ms = 1800);

// ---------------------------------------------------------------- theme

struct Theme {
  uint16_t bg;        // screen background
  uint16_t surface;   // boxes, unselected keys
  uint16_t bar;       // tab bar / title bar
  uint16_t text;
  uint16_t textDim;
  uint16_t accent;    // selection, highlights
  uint16_t onAccent;  // text on accent
  uint16_t select;    // selected row background
  uint16_t ok;
  uint16_t warn;
  uint16_t bad;
};
const Theme& theme();

constexpr uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) {
  return static_cast<uint16_t>(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
}

// Fonts (all from LovyanGFX).
const lgfx::IFont* fontBody();   // DejaVu9: menus, labels
const lgfx::IFont* fontLarge();  // DejaVu12: titles, values
const lgfx::IFont* fontHuge();   // DejaVu18: big numbers
const lgfx::IFont* fontSmall();  // Font0 6x8: footer
const lgfx::IFont* fontLogo();   // Orbitron Light 24: "SELENO"

int16_t screenW();
int16_t screenH();
constexpr int16_t kFooterH = 12;
constexpr int16_t kBarH = 16;

// --------------------------------------------------------------- drawing

// Cut s to fit maxWidth pixels in the current font, ending in "..".
String fitText(Gfx& g, const String& s, int32_t maxWidth);
// Filled triangle pointing 'l', 'r', 'u' or 'd', centred on (cx, cy).
void drawArrow(Gfx& g, int32_t cx, int32_t cy, int32_t size, char dir, uint16_t color);
void drawToggle(Gfx& g, int32_t x, int32_t cy, bool on, bool focused);
// A bar with a title (left) and optional text (right).
void drawTitleBar(Gfx& g, const String& title, const String& right = "");
// Dialogs about the Sun: an amber (Warn) or red (Danger) title bar with a
// Sun symbol.
enum class Alert : uint8_t { None, Warn, Danger };
void drawAlertBar(Gfx& g, const String& title, Alert level);
void drawSun(Gfx& g, int32_t cx, int32_t cy, int32_t r, uint16_t color);
void drawFooter(Gfx& g, const String& hint);
// Font0 text where \x07 is the SELECT key, \x1b RETURN, and \x10 \x11
// \x1e \x1f are right/left/up/down arrows (drawn, since LovyanGFX skips
// control characters). x is the left edge, cy the vertical centre.
int32_t iconTextWidth(Gfx& g, const String& s);
void drawIconText(Gfx& g, const String& s, int32_t x, int32_t cy, uint16_t color);
// Text with '*' drawn as a degree sign (DejaVu has no glyph for it).
// y is the top of the text.
void drawDegreesText(Gfx& g, const String& s, int32_t x, int32_t y, uint16_t color);
// Word-wrapped text inside a box; returns the height used.
int32_t drawWrapped(Gfx& g, const String& s, int32_t x, int32_t y, int32_t w, uint16_t color, int32_t lineH);
// The SELENO crescent, phase 0..1 animates it.
void drawMoon(Gfx& g, int32_t cx, int32_t cy, int32_t r, float phase);

// ------------------------------------------------------- shared settings

struct Settings {
  uint8_t brightness = 80;  // percent
  uint8_t rotation = 0;     // UI orientation; 0 = portrait like the host's
  uint8_t theme = 0;        // 0 dark, 1 night (red)
  uint16_t dimAfterS = 120; // 0 = never
  uint8_t dimLevel = 8;     // percent while dimmed
  bool ledsMuted = false;
  uint8_t jogSpeed = 1;     // index into kJogSpeeds (ui_panels.cpp)
  bool skyRadar = true;     // radar inset on the sky view
};
Settings& settings();
void saveSettings();
// Re-apply brightness/rotation/theme after a settings change.
void applySettings();

// --------------------------------------------------------- link and host

enum class Link : uint8_t { Waiting, Connected, Lost };
Link link();
const char* linkName(Link l);
// What the Pi told us in hello/status (empty until then).
const String& deviceName();
const String& hostStatus(const char* key);
void forEachHostStatus(std::function<void(const String&, const String&)> fn);
// Local time from the Pi's status, "" when unknown.
String clockText();
// Send {"event":"ui", type, ...}; fill extra fields in `doc` first.
void emitUiEvent(JsonDocument& doc, const char* type);

// Core internals for ui_host.cpp.
void injectKey(Key k, bool pressed);
void stackJson(JsonArray out);
void noteHello(const char* device);
void noteStatus(JsonObjectConst fields);
void statusJson(JsonObject out);
bool hostMode();
void leaveHostMode();
LGFX_Sprite* renderForScreenshot();
void wakeScreen();

// Pi-driven screens (ui_screens.cpp).
void showLoading();
void showMenu();
void showTracking();
bool trackingActive();
// The menu's root screen (ui_menu.cpp).
Screen* menuScreen();

}  // namespace ui
