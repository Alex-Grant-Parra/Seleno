// UI core: screen stack, key handling, dimming, the Pi link state,
// settings and the render loop.
#include <Preferences.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#include <vector>

#include "config.h"
#include "display.h"
#include "estop.h"
#include "front_panel.h"
#include "host_link.h"
#include "ui.h"
#include "ui_internal.h"
#include "ui_menu.h"
#include "ui_screens.h"

namespace ui {

// Defined in ui_host.cpp and ui_panels.cpp.
void hostPollPrompts();
void buildLocalPanels();

namespace {

constexpr uint32_t kLinkTimeoutMs = 6000;
constexpr uint32_t kMinFrameMs = 25;
constexpr int kMaxStack = 8;

struct RawKey {
  uint8_t key;
  bool pressed;
  bool injected;  // from {"cmd":"ui","action":"key"}
  uint32_t atMs;
};

struct KeyState {
  bool held = false;
  bool injected = false;
  bool longFired = false;
  bool swallow = false;  // the press only woke the screen
  uint32_t pressedAt = 0;
  uint32_t lastRepeatAt = 0;
  uint32_t upSinceMs = 0;  // panel says released, but no release event yet
};

QueueHandle_t g_keyQueue = nullptr;
KeyState g_keys[static_cast<int>(Key::Count)];
uint32_t g_lastReturnPressMs = 0;

Screen* g_stack[kMaxStack];
int g_depth = 0;
bool g_dirty = true;
uint32_t g_lastDrawMs = 0;
String g_lastClock;

String g_toast;
uint32_t g_toastUntil = 0;

bool g_hostMode = false;  // the host draws on the panel directly
bool g_dimmed = false;
uint32_t g_lastActivityMs = 0;

uint32_t g_helloAtMs = 0;
Link g_lastLink = Link::Waiting;
String g_deviceName;
std::vector<std::pair<String, String>> g_status;
String g_empty;
int32_t g_clockSeconds = -1;  // local seconds after midnight at g_clockAtMs
uint32_t g_clockAtMs = 0;

uint32_t g_seenPanelStops = 0;

Settings g_settings;
Preferences g_prefs;
bool g_prefsOpen = false;

LGFX_Sprite g_frame;
bool g_frameOk = false;
bool g_panelOk = false;

const Theme kDark = {
    rgb(4, 6, 12),       // bg
    rgb(24, 30, 46),     // surface
    rgb(16, 21, 34),     // bar
    rgb(228, 232, 240),  // text
    rgb(122, 132, 153),  // textDim
    rgb(242, 193, 78),   // accent: moon gold
    rgb(20, 16, 4),      // onAccent
    rgb(36, 48, 77),     // select
    rgb(74, 222, 128),   // ok
    rgb(251, 191, 36),   // warn
    rgb(248, 113, 113),  // bad
};

// Night: red only, nothing bright, to keep dark adaptation.
const Theme kNight = {
    rgb(0, 0, 0),       rgb(40, 0, 0),   rgb(24, 0, 0),   rgb(220, 30, 20),
    rgb(120, 16, 12),   rgb(255, 60, 40), rgb(20, 0, 0),  rgb(70, 0, 0),
    rgb(200, 40, 20),   rgb(220, 50, 20), rgb(255, 30, 30),
};

Key keyFromButton(Button b) {
  switch (b) {
    case Button::Select: return Key::Select;
    case Button::Return: return Key::Return;
    case Button::Top: return Key::Up;
    case Button::Bottom: return Key::Down;
    case Button::Left: return Key::Left;
    case Button::Right: return Key::Right;
    default: return Key::Count;
  }
}

void onButton(Button b, bool pressed, uint32_t atMs) {
  Key k = keyFromButton(b);
  if (k == Key::Count || g_keyQueue == nullptr) {
    return;
  }
  RawKey raw{static_cast<uint8_t>(k), pressed, false, atMs};
  xQueueSend(g_keyQueue, &raw, 0);
}

bool isDirection(Key k) {
  return k == Key::Up || k == Key::Down || k == Key::Left || k == Key::Right;
}

void deliver(Key k, KeyAction action, bool doublePress, uint32_t atMs) {
  Screen* s = top();
  if (s == nullptr) {
    return;
  }
  KeyEvent e{k, action, doublePress, atMs};
  s->key(e);
  g_dirty = true;
}

void wake(uint32_t now) {
  g_lastActivityMs = now;
  if (g_dimmed) {
    g_dimmed = false;
    applySettings();
  }
}

void handleRaw(const RawKey& raw) {
  Key k = static_cast<Key>(raw.key);
  KeyState& st = g_keys[raw.key];
  uint32_t now = millis();

  if (raw.pressed) {
    bool wasDimmed = g_dimmed;
    wake(now);
    st.held = true;
    st.injected = raw.injected;
    st.longFired = false;
    st.pressedAt = now;
    st.lastRepeatAt = now;
    st.upSinceMs = 0;
    st.swallow = wasDimmed;

    bool doublePress = false;
    if (k == Key::Return) {
      doublePress = g_lastReturnPressMs != 0 && raw.atMs - g_lastReturnPressMs <= kReturnDoublePressMs;
      g_lastReturnPressMs = doublePress ? 0 : (raw.atMs == 0 ? 1 : raw.atMs);
      if (g_hostMode) {
        // RETURN hands the screen back from the host. A double press there
        // is the panel task's business (pollPanelStop shows the menu).
        g_hostMode = false;
        applySettings();
        st.swallow = true;
      }
    }
    if (st.swallow) {
      return;
    }
    if (k != Key::Select) {
      deliver(k, KeyAction::Press, doublePress, raw.atMs);
    }
    return;
  }

  // Release
  bool wasHeld = st.held;
  st.held = false;
  if (!wasHeld || st.swallow) {
    st.swallow = false;
    return;
  }
  if (k == Key::Select && !st.longFired) {
    deliver(k, KeyAction::Press, false, raw.atMs);
  }
}

void pollHeldKeys(uint32_t now) {
  for (int i = 0; i < static_cast<int>(Key::Count); i++) {
    KeyState& st = g_keys[i];
    if (!st.held || st.swallow) {
      continue;
    }
    Key k = static_cast<Key>(i);
    if (isDirection(k)) {
      if (now - st.pressedAt >= kRepeatDelayMs && now - st.lastRepeatAt >= kRepeatIntervalMs) {
        st.lastRepeatAt = now;
        deliver(k, KeyAction::Repeat, false, now);
      }
    } else if (!st.longFired && now - st.pressedAt >= kLongPressMs) {
      st.longFired = true;
      deliver(k, KeyAction::Long, false, now);
    }
  }
  // A release we never heard about (the expander dropped off the bus while
  // a key was down) would leave a key repeating forever. Key i and
  // Button i share their order, so the panel's bit says if it's still down.
  uint16_t down = panelPressedButtons();
  for (int i = 0; i < static_cast<int>(Key::Count); i++) {
    KeyState& st = g_keys[i];
    if (!st.held || st.injected) {
      continue;
    }
    if (down & (1u << i)) {
      st.upSinceMs = 0;
    } else if (st.upSinceMs == 0) {
      st.upSinceMs = now == 0 ? 1 : now;
    } else if (now - st.upSinceMs > 250) {
      st.held = false;
      st.upSinceMs = 0;
    }
  }
}

void loadSettings() {
  g_prefsOpen = g_prefs.begin("ui", false);
  if (!g_prefsOpen) {
    return;
  }
  g_settings.brightness = g_prefs.getUChar("bright", g_settings.brightness);
  g_settings.rotation = g_prefs.getUChar("rot", g_settings.rotation) & 3;
  g_settings.theme = g_prefs.getUChar("theme", g_settings.theme);
  g_settings.dimAfterS = g_prefs.getUShort("dim", g_settings.dimAfterS);
  g_settings.ledsMuted = g_prefs.getBool("ledmute", g_settings.ledsMuted);
  g_settings.jogSpeed = g_prefs.getUChar("jog", g_settings.jogSpeed);
  g_settings.skyRadar = g_prefs.getBool("radar", g_settings.skyRadar);
  if (g_settings.brightness < 5) g_settings.brightness = 5;
  if (g_settings.brightness > 100) g_settings.brightness = 100;
}

void ensureFrame() {
  int16_t w = (g_settings.rotation & 1) ? kLcdHeight : kLcdWidth;
  int16_t h = (g_settings.rotation & 1) ? kLcdWidth : kLcdHeight;
  if (g_frameOk && g_frame.width() == w && g_frame.height() == h) {
    return;
  }
  if (g_frameOk) {
    g_frame.deleteSprite();
  }
  g_frame.setColorDepth(16);
  g_frameOk = g_frame.createSprite(w, h) != nullptr;
}

void drawToast(Gfx& g) {
  const Theme& t = theme();
  g.setFont(fontBody());
  int32_t w = screenW() - 12;
  int32_t lines = 1 + g.textWidth(g_toast) / (w - 8);
  if (lines > 3) lines = 3;
  int32_t h = lines * 11 + 8;
  int32_t y = screenH() - kFooterH - h - 4;
  g.fillRoundRect(6, y, w, h, 4, t.surface);
  g.drawRoundRect(6, y, w, h, 4, t.accent);
  drawWrapped(g, g_toast, 10, y + 4, w - 8, t.text, 11);
}

void render(uint32_t now) {
  Screen* s = top();
  if (s == nullptr || !g_frameOk) {
    return;
  }
  Gfx& g = g_frame;
  g.setTextWrap(false);
  g.fillScreen(theme().bg);
  s->draw(g);
  if (s->wantsFooter()) {
    drawFooter(g, s->footerHint());
  }
  if (g_toast.length() > 0 && now < g_toastUntil) {
    drawToast(g);
  }
  if (g_panelOk) {
    displayUseRotation(g_settings.rotation);
    g.pushSprite(&displayDevice(), 0, 0);
  }
  g_lastDrawMs = now;
  g_dirty = false;
}

void pollLink(uint32_t now) {
  Link l = link();
  if (l == g_lastLink) {
    return;
  }
  Link was = g_lastLink;
  g_lastLink = l;
  g_dirty = true;
  if (l == Link::Lost) {
    toast("Lost connection to the Pi");
  } else if (l == Link::Connected && was == Link::Lost) {
    toast("Pi connected again");
  }
}

void pollDimming(uint32_t now) {
  if (g_hostMode || g_dimmed || g_settings.dimAfterS == 0) {
    return;
  }
  if (now - g_lastActivityMs >= static_cast<uint32_t>(g_settings.dimAfterS) * 1000U) {
    g_dimmed = true;
    displayBacklight(static_cast<uint8_t>(g_settings.dimLevel * 255U / 100U));
  }
}

// The buttons hang off the MCP23017; if it drops off the bus, RETURN can't
// stop anything, so say so.
void pollExpander() {
  static int8_t last = -1;
  int8_t present = panelExpanderPresent() ? 1 : 0;
  if (last >= 0 && present != last) {
    toast(present ? "Buttons are back" : "Buttons not responding (I/O expander lost)", 4000);
  }
  last = present;
}

void pollPanelStop() {
  uint32_t count = panelStopCount();
  if (count == g_seenPanelStops) {
    return;
  }
  g_seenPanelStops = count;
  wake(millis());
  g_hostMode = false;
  showMenu();
  toast("Stopped. Motors halted from the panel.");
}

}  // namespace

// ------------------------------------------------------------ public API

const char* keyName(Key key) {
  switch (key) {
    case Key::Select: return "select";
    case Key::Return: return "return";
    case Key::Up: return "up";
    case Key::Down: return "down";
    case Key::Left: return "left";
    case Key::Right: return "right";
    default: return "";
  }
}

bool keyHeld(Key key) {
  int i = static_cast<int>(key);
  return i >= 0 && i < static_cast<int>(Key::Count) && g_keys[i].held;
}

bool parseKey(const char* name, Key& out) {
  if (name == nullptr) return false;
  struct Alias {
    const char* name;
    Key key;
  };
  static const Alias kAliases[] = {
      {"select", Key::Select}, {"enter", Key::Select}, {"return", Key::Return}, {"back", Key::Return},
      {"up", Key::Up},         {"top", Key::Up},       {"down", Key::Down},     {"bottom", Key::Down},
      {"left", Key::Left},     {"right", Key::Right},
  };
  for (const Alias& a : kAliases) {
    if (strcmp(a.name, name) == 0) {
      out = a.key;
      return true;
    }
  }
  return false;
}

void injectKey(Key k, bool pressed) {
  if (g_keyQueue == nullptr) return;
  uint32_t now = millis();
  if (pressed && k == Key::Return) {
    // Same safety policy as a real press (the panel task does this itself).
    returnButtonPressed(now);
  }
  RawKey raw{static_cast<uint8_t>(k), pressed, true, now};
  xQueueSend(g_keyQueue, &raw, 0);
}

void push(Screen* s) {
  if (s == nullptr || g_depth >= kMaxStack) return;
  g_stack[g_depth++] = s;
  s->enter();
  g_dirty = true;
}

void pop() {
  if (g_depth <= 1) return;  // the root stays
  Screen* s = g_stack[--g_depth];
  s->leave();
  g_dirty = true;
}

void popTo(Screen* s) {
  if (!onStack(s)) return;
  while (g_depth > 0 && g_stack[g_depth - 1] != s) {
    g_stack[--g_depth]->leave();
  }
  g_dirty = true;
}

void setRoot(Screen* s) {
  while (g_depth > 0) {
    g_stack[--g_depth]->leave();
  }
  push(s);
}

Screen* top() {
  return g_depth > 0 ? g_stack[g_depth - 1] : nullptr;
}

bool onStack(const Screen* s) {
  for (int i = 0; i < g_depth; i++) {
    if (g_stack[i] == s) return true;
  }
  return false;
}

void stackJson(JsonArray out) {
  for (int i = 0; i < g_depth; i++) {
    out.add(g_stack[i]->name());
  }
}

void invalidate() {
  g_dirty = true;
}

void toast(const String& text, uint32_t ms) {
  g_toast = text;
  g_toastUntil = millis() + ms;
  g_dirty = true;
}

const Theme& theme() {
  return g_settings.theme == 1 ? kNight : kDark;
}

int16_t screenW() {
  return g_frameOk ? g_frame.width() : kLcdWidth;
}

int16_t screenH() {
  return g_frameOk ? g_frame.height() : kLcdHeight;
}

Settings& settings() {
  return g_settings;
}

void saveSettings() {
  if (!g_prefsOpen) return;
  g_prefs.putUChar("bright", g_settings.brightness);
  g_prefs.putUChar("rot", g_settings.rotation);
  g_prefs.putUChar("theme", g_settings.theme);
  g_prefs.putUShort("dim", g_settings.dimAfterS);
  g_prefs.putBool("ledmute", g_settings.ledsMuted);
  g_prefs.putUChar("jog", g_settings.jogSpeed);
  g_prefs.putBool("radar", g_settings.skyRadar);
}

void applySettings() {
  ensureFrame();
  if (!g_dimmed && !g_hostMode) {
    displayBacklight(static_cast<uint8_t>(g_settings.brightness * 255U / 100U));
  }
  ledsSetMuted(g_settings.ledsMuted);
  g_dirty = true;
}

Link link() {
  if (g_helloAtMs == 0) {
    return Link::Waiting;
  }
  return millis() - hostLastRxMs() > kLinkTimeoutMs ? Link::Lost : Link::Connected;
}

const char* linkName(Link l) {
  switch (l) {
    case Link::Waiting: return "waiting";
    case Link::Connected: return "connected";
    case Link::Lost: return "lost";
  }
  return "";
}

const String& deviceName() {
  return g_deviceName;
}

const String& hostStatus(const char* key) {
  for (const auto& kv : g_status) {
    if (kv.first == key) return kv.second;
  }
  return g_empty;
}

void forEachHostStatus(std::function<void(const String&, const String&)> fn) {
  for (const auto& kv : g_status) {
    fn(kv.first, kv.second);
  }
}

String clockText() {
  if (g_clockSeconds < 0) {
    return "";
  }
  uint32_t secs = (static_cast<uint32_t>(g_clockSeconds) + (millis() - g_clockAtMs) / 1000U) % 86400U;
  char buf[8];
  snprintf(buf, sizeof(buf), "%02u:%02u", static_cast<unsigned>(secs / 3600), static_cast<unsigned>((secs / 60) % 60));
  return buf;
}

void emitUiEvent(JsonDocument& doc, const char* type) {
  doc["event"] = "ui";
  doc["type"] = type;
  hostEmitEvent(doc);
}

// Called from ui_host.cpp.
void noteHello(const char* device) {
  bool first = g_helloAtMs == 0;
  g_helloAtMs = millis();
  if (device != nullptr) {
    g_deviceName = device;
  }
  g_lastLink = Link::Connected;
  if (first || !onStack(menuScreen())) {
    // Leave the loading screen. The Pi may follow with a prompt.
    showMenu();
  }
  g_dirty = true;
}

void noteStatus(JsonObjectConst fields) {
  for (JsonPairConst kv : fields) {
    const char* key = kv.key().c_str();
    if (strcmp(key, "cmd") == 0 || strcmp(key, "action") == 0 || strcmp(key, "id") == 0) {
      continue;
    }
    if (strcmp(key, "clock_s") == 0) {
      g_clockSeconds = kv.value().as<int32_t>();
      g_clockAtMs = millis();
      continue;
    }
    if (strcmp(key, "device") == 0) {
      g_deviceName = kv.value().as<const char*>();
      continue;
    }
    String value = kv.value().is<const char*>() ? String(kv.value().as<const char*>()) : kv.value().as<String>();
    bool found = false;
    for (auto& existing : g_status) {
      if (existing.first == key) {
        if (existing.second != value) g_dirty = true;
        existing.second = value;
        found = true;
        break;
      }
    }
    if (!found && g_status.size() < 24) {
      g_status.emplace_back(String(key), value);
      g_dirty = true;
    }
  }
}

void statusJson(JsonObject out) {
  out["link"] = linkName(link());
  out["host_mode"] = g_hostMode;
  out["dimmed"] = g_dimmed;
  out["device"] = g_deviceName;
  out["panel_stop_pending"] = panelStopPending();
  out["return_navigates"] = returnButtonNavigates();
  out["toast"] = millis() < g_toastUntil ? g_toast : String("");
  out["clock"] = clockText();
}

bool hostMode() {
  return g_hostMode;
}

void leaveHostMode() {
  if (g_hostMode) {
    g_hostMode = false;
    applySettings();
  }
  g_dirty = true;
}

// Render now into the frame (screenshots).
LGFX_Sprite* renderForScreenshot() {
  if (!g_frameOk) return nullptr;
  render(millis());
  return &g_frame;
}

void wakeScreen() {
  wake(millis());
}

}  // namespace ui

// ------------------------------------------------------------ outside API

void uiBegin() {
  using namespace ui;
  loadSettings();
  g_keyQueue = xQueueCreate(16, sizeof(RawKey));
  panelSetButtonListener(onButton);
  g_panelOk = displayInit();
  ensureFrame();
  g_lastActivityMs = millis();
  g_seenPanelStops = panelStopCount();
  buildLocalPanels();
  applySettings();
  showLoading();
  render(millis());
}

void uiPoll() {
  using namespace ui;
  uint32_t now = millis();

  RawKey raw;
  while (g_keyQueue != nullptr && xQueueReceive(g_keyQueue, &raw, 0) == pdTRUE) {
    handleRaw(raw);
  }
  pollHeldKeys(now);
  pollPanelStop();
  pollExpander();
  pollLink(now);
  pollDimming(now);
  screensPoll();
  hostPollPrompts();

  Screen* s = top();
  if (s != nullptr) {
    s->tick(now);
    s = top();
  }
  returnButtonSetNavigates(s != nullptr && s->returnNavigates() && !g_hostMode);

  if (g_hostMode || s == nullptr) {
    return;
  }
  if (g_toastUntil != 0 && now >= g_toastUntil) {
    g_toastUntil = 0;
    g_dirty = true;
  }
  if (now - g_lastDrawMs >= 1000) {
    String clock = clockText();
    if (clock != g_lastClock) {
      g_lastClock = clock;
      g_dirty = true;
    }
  }
  uint32_t refresh = s->refreshMs();
  bool due = g_dirty || (refresh > 0 && now - g_lastDrawMs >= refresh);
  if (due && now - g_lastDrawMs >= kMinFrameMs) {
    render(now);
  }
}

void uiHostDrawing() {
  using namespace ui;
  if (!g_hostMode) {
    g_hostMode = true;
    // The host's assets assume its own orientation and full brightness.
    displayUseRotation(displayHostRotation());
    if (g_dimmed) {
      g_dimmed = false;
    }
    displayBacklight(static_cast<uint8_t>(g_settings.brightness * 255U / 100U));
  }
}
