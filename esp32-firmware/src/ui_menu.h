// The menu: panels shown as tabs across the top, each with a list of items.
//
// Adding a firmware panel (see ui_panels.cpp for real examples):
//
//   PanelBuilder p = addPanel("lights", "Lights", 35);   // id, tab, order
//   p.toggle("led", "Red LED", [] { return ledState(5); },
//            [](bool on) { ledSet(5, on); });
//   p.choice("mode", "Mode", {"Slow", "Fast"}, getter, setter);
//   p.number("speed", "Speed", 1, 100, 1, "%", getter, setter);
//   p.action("go", "Do it", [] { ... }).confirm = "Really?";
//   p.info("temp", "Temp", [] { return String(42); });
//   p.screen("test", "Fancy test", &myScreen);
//
// Panels from the Pi arrive as JSON ({"cmd":"ui","action":"menu"}) and use
// the same item kinds; their changes go back as {"event":"ui"} lines. Tabs
// are sorted by `order` (firmware and Pi panels mix).
#pragma once

#include <functional>
#include <initializer_list>
#include <vector>

#include "ui_internal.h"

namespace ui {

enum class ItemKind : uint8_t {
  Action,  // SELECT runs it (after `confirm`, if set)
  Toggle,  // on/off; SELECT or LEFT/RIGHT flip it
  Choice,  // one of `options`; LEFT/RIGHT step through them
  Number,  // min..max in `step`s; LEFT/RIGHT change it
  Text,    // SELECT opens the keyboard
  Info,    // read-only label + value
  Screen,  // SELECT opens a full-screen view (tests)
};

const char* itemKindName(ItemKind kind);
bool parseItemKind(const char* name, ItemKind& out);

struct MenuItem;
using ItemHook = std::function<void(MenuItem&)>;

struct MenuItem {
  ItemKind kind = ItemKind::Action;
  String id;
  String label;
  String text;           // Text value; Info value
  int32_t value = 0;     // Toggle 0/1, Choice index, Number value
  int32_t minValue = 0;  // Number
  int32_t maxValue = 100;
  int32_t step = 1;
  uint8_t decimals = 0;  // Number: value / 10^decimals is shown
  String unit;           // Number suffix
  std::vector<String> options;  // Choice
  uint16_t maxLength = 64;      // Text
  bool secret = false;   // Text: shown as dots outside the editor
  String confirm;        // Action/Toggle: ask this first
  String hint;           // shown in the footer when selected
  bool enabled = true;
  bool visible = true;
  ui::Screen* screen = nullptr;  // Screen items
  // Firmware items: pull the live value before drawing, and react to a
  // change, an action or opening. Pi items have neither; the menu emits
  // events for them instead.
  ItemHook refresh;
  ItemHook changed;

  String valueText() const;
  bool adjustable() const { return kind == ItemKind::Toggle || kind == ItemKind::Choice || kind == ItemKind::Number; }
};

struct MenuPanel {
  String id;
  String title;
  int order = 50;
  bool remote = false;  // defined by the Pi
  std::vector<MenuItem> items;

  MenuItem* find(const char* itemId);
};

// Fluent helpers for firmware panels. Each returns the new item so callers
// can set extra fields (confirm, hint, decimals...).
class PanelBuilder {
 public:
  explicit PanelBuilder(MenuPanel& panel) : panel_(panel) {}

  MenuItem& action(const char* id, const char* label, std::function<void()> run);
  MenuItem& toggle(const char* id, const char* label, std::function<bool()> get, std::function<void(bool)> set);
  MenuItem& choice(const char* id, const char* label, std::initializer_list<const char*> options,
                   std::function<int()> get, std::function<void(int)> set);
  MenuItem& number(const char* id, const char* label, int32_t minValue, int32_t maxValue, int32_t step,
                   const char* unit, std::function<int32_t()> get, std::function<void(int32_t)> set);
  MenuItem& text(const char* id, const char* label, uint16_t maxLength, std::function<String()> get,
                 std::function<void(const String&)> set);
  MenuItem& info(const char* id, const char* label, std::function<String()> get);
  MenuItem& screen(const char* id, const char* label, ui::Screen* s);

 private:
  MenuItem& add(ItemKind kind, const char* id, const char* label);
  MenuPanel& panel_;
};

// Firmware panels: call from buildLocalPanels() (ui_panels.cpp) at boot.
PanelBuilder addPanel(const char* id, const char* title, int order);

// Pi panels: replace them all from a JSON array (see PROTOCOL.md).
// Returns false with *error set if the JSON is unusable.
bool setRemotePanels(JsonArrayConst panels, String* error);
// Update Pi item values: {"panel.item": value, ...}. Unknown keys are
// skipped. Returns how many items changed.
int setRemoteValues(JsonObjectConst values);
// Change one item's properties from JSON (value, label, enabled, ...).
bool updateRemoteItem(const char* panelId, const char* itemId, JsonObjectConst fields);

MenuPanel* findPanel(const char* id);
int panelCount();
MenuPanel& panelAt(int index);

// Open a panel (and optionally an item) in the menu.
bool menuShowPanel(const char* panelId, const char* itemId);
void menuStateJson(JsonObject out);

// Apply a new value to an item: runs its hook (firmware) or emits a change
// event (Pi). Used by the menu and the keyboard.
void commitItem(MenuPanel& panel, MenuItem& item);

}  // namespace ui
