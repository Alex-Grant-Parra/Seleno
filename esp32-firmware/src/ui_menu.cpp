// Menu model, Pi panels, and the menu screen (tabs + item list).
#include "ui_menu.h"

#include <algorithm>
#include <cmath>

#include "ui_dialogs.h"

namespace ui {

namespace {

std::vector<MenuPanel*> g_panels;  // sorted by order; heap so pointers stay put
String g_selectedPanelId;          // survives Pi menu updates

void sortPanels() {
  std::stable_sort(g_panels.begin(), g_panels.end(),
                   [](const MenuPanel* a, const MenuPanel* b) { return a->order < b->order; });
}

int32_t pow10i(uint8_t decimals) {
  int32_t p = 1;
  for (uint8_t i = 0; i < decimals; i++) p *= 10;
  return p;
}

bool remoteUsable(const MenuPanel& panel) {
  return !panel.remote || link() == Link::Connected;
}

// Pi item value from JSON, typed by the item's kind.
void applyJsonValue(MenuItem& item, JsonVariantConst v) {
  switch (item.kind) {
    case ItemKind::Toggle:
      item.value = v.as<bool>() ? 1 : 0;
      break;
    case ItemKind::Choice:
      if (v.is<const char*>()) {
        const char* s = v.as<const char*>();
        for (size_t i = 0; i < item.options.size(); i++) {
          if (item.options[i] == s) item.value = static_cast<int32_t>(i);
        }
      } else {
        item.value = v.as<int32_t>();
      }
      if (item.value < 0) item.value = 0;
      if (!item.options.empty() && item.value >= static_cast<int32_t>(item.options.size())) {
        item.value = static_cast<int32_t>(item.options.size()) - 1;
      }
      break;
    case ItemKind::Number:
      item.value = static_cast<int32_t>(lround(v.as<double>() * pow10i(item.decimals)));
      break;
    case ItemKind::Text:
    case ItemKind::Info:
      item.text = v.is<const char*>() ? String(v.as<const char*>()) : v.as<String>();
      break;
    default:
      break;
  }
}

void applyJsonFields(MenuItem& item, JsonObjectConst o) {
  if (o["label"].is<const char*>()) item.label = o["label"].as<const char*>();
  if (o["options"].is<JsonArrayConst>()) {
    item.options.clear();
    for (JsonVariantConst opt : o["options"].as<JsonArrayConst>()) {
      item.options.push_back(opt.as<String>());
    }
  }
  if (!o["min"].isNull()) item.minValue = static_cast<int32_t>(lround(o["min"].as<double>() * pow10i(item.decimals)));
  if (!o["max"].isNull()) item.maxValue = static_cast<int32_t>(lround(o["max"].as<double>() * pow10i(item.decimals)));
  if (!o["step"].isNull()) {
    item.step = static_cast<int32_t>(lround(o["step"].as<double>() * pow10i(item.decimals)));
    if (item.step < 1) item.step = 1;
  }
  if (o["unit"].is<const char*>()) item.unit = o["unit"].as<const char*>();
  if (!o["max_length"].isNull()) item.maxLength = o["max_length"].as<uint16_t>();
  if (!o["secret"].isNull()) item.secret = o["secret"].as<bool>();
  if (o["confirm"].is<const char*>()) item.confirm = o["confirm"].as<const char*>();
  if (o["hint"].is<const char*>()) item.hint = o["hint"].as<const char*>();
  if (!o["enabled"].isNull()) item.enabled = o["enabled"].as<bool>();
  if (!o["visible"].isNull()) item.visible = o["visible"].as<bool>();
  if (!o["value"].isNull()) applyJsonValue(item, o["value"]);
}

bool parseRemoteItem(JsonObjectConst o, MenuItem& item, String* error) {
  const char* id = o["id"] | "";
  const char* type = o["type"] | "action";
  if (id[0] == '\0') {
    if (error) *error = "item without id";
    return false;
  }
  if (!parseItemKind(type, item.kind) || item.kind == ItemKind::Screen) {
    if (error) *error = String("bad item type: ") + type;
    return false;
  }
  item.id = id;
  item.label = o["label"] | id;
  item.decimals = o["decimals"] | 0;
  if (item.decimals > 4) item.decimals = 4;
  applyJsonFields(item, o);
  return true;
}

void emitChange(const MenuPanel& panel, const MenuItem& item) {
  JsonDocument doc;
  doc["panel"] = panel.id;
  doc["item"] = item.id;
  switch (item.kind) {
    case ItemKind::Action:
      emitUiEvent(doc, "action");
      return;
    case ItemKind::Toggle:
      doc["value"] = item.value != 0;
      break;
    case ItemKind::Choice:
      doc["value"] = item.value;
      if (item.value >= 0 && item.value < static_cast<int32_t>(item.options.size())) {
        doc["option"] = item.options[item.value];
      }
      break;
    case ItemKind::Number:
      if (item.decimals == 0) {
        doc["value"] = item.value;
      } else {
        doc["value"] = static_cast<double>(item.value) / pow10i(item.decimals);
      }
      break;
    case ItemKind::Text:
      doc["value"] = item.text;
      break;
    default:
      return;
  }
  emitUiEvent(doc, "change");
}

// ------------------------------------------------------------ menu screen

class MenuScreen : public Screen {
 public:
  const char* name() const override { return "menu"; }
  uint32_t refreshMs() const override { return 500; }

  void enter() override { restoreSelection(); }

  void key(const KeyEvent& e) override {
    if (g_panels.empty()) return;
    restoreSelection();
    if (editing_) {
      editKey(e);
      return;
    }
    if (focusTabs_) {
      tabsKey(e);
    } else {
      listKey(e);
    }
  }

  void draw(Gfx& g) override {
    restoreSelection();
    drawTabs(g);
    drawList(g);
  }

  String footerHint() const override;

  void stateJson(JsonObject out) const override {
    const MenuPanel* p = panel();
    out["panel"] = p != nullptr ? p->id : String("");
    out["focus"] = focusTabs_ ? "tabs" : "list";
    out["editing"] = editing_;
    const MenuItem* it = item();
    out["item"] = it != nullptr ? it->id : String("");
    out["item_index"] = itemIndex_;
  }

  void showPanel(const char* panelId, const char* itemId) {
    for (size_t i = 0; i < g_panels.size(); i++) {
      if (g_panels[i]->id == panelId) {
        panelIndex_ = static_cast<int>(i);
        g_selectedPanelId = g_panels[i]->id;
        itemIndex_ = 0;
        scroll_ = 0;
        editing_ = false;
        focusTabs_ = true;
        if (itemId != nullptr && itemId[0] != '\0') {
          std::vector<int> vis = visibleItems();
          for (size_t k = 0; k < vis.size(); k++) {
            if (g_panels[i]->items[vis[k]].id == itemId) {
              itemIndex_ = static_cast<int>(k);
              focusTabs_ = false;
            }
          }
        }
        invalidate();
        return;
      }
    }
  }

  void resetFocus() {
    focusTabs_ = true;
    editing_ = false;
  }

 private:
  int panelIndex_ = 0;
  int itemIndex_ = 0;  // index into visibleItems()
  int scroll_ = 0;     // first visible row
  bool focusTabs_ = true;
  bool editing_ = false;
  int32_t editOriginal_ = 0;
  int32_t tabScroll_ = 0;

  static constexpr int16_t kRowH = 15;

  MenuPanel* panel() const {
    if (panelIndex_ < 0 || panelIndex_ >= static_cast<int>(g_panels.size())) return nullptr;
    return g_panels[panelIndex_];
  }

  std::vector<int> visibleItems() const {
    std::vector<int> out;
    MenuPanel* p = panel();
    if (p == nullptr) return out;
    for (size_t i = 0; i < p->items.size(); i++) {
      if (p->items[i].visible) out.push_back(static_cast<int>(i));
    }
    return out;
  }

  MenuItem* item() const {
    std::vector<int> vis = visibleItems();
    if (itemIndex_ < 0 || itemIndex_ >= static_cast<int>(vis.size())) return nullptr;
    return &panel()->items[vis[itemIndex_]];
  }

  // Pi menus can be replaced at any time; keep pointing at the same panel.
  void restoreSelection() {
    if (g_panels.empty()) {
      panelIndex_ = 0;
      return;
    }
    if (panelIndex_ >= static_cast<int>(g_panels.size()) || g_panels[panelIndex_]->id != g_selectedPanelId) {
      int found = -1;
      for (size_t i = 0; i < g_panels.size(); i++) {
        if (g_panels[i]->id == g_selectedPanelId) found = static_cast<int>(i);
      }
      if (found < 0) {
        panelIndex_ = std::min<int>(panelIndex_, static_cast<int>(g_panels.size()) - 1);
        itemIndex_ = 0;
        scroll_ = 0;
        editing_ = false;
      } else {
        panelIndex_ = found;
      }
      g_selectedPanelId = g_panels[panelIndex_]->id;
    }
    int n = static_cast<int>(visibleItems().size());
    if (itemIndex_ >= n) itemIndex_ = n > 0 ? n - 1 : 0;
    if (n == 0) {
      focusTabs_ = true;
      editing_ = false;
    }
  }

  void selectPanel(int index) {
    int n = static_cast<int>(g_panels.size());
    panelIndex_ = ((index % n) + n) % n;
    g_selectedPanelId = g_panels[panelIndex_]->id;
    itemIndex_ = 0;
    scroll_ = 0;
  }

  void tabsKey(const KeyEvent& e) {
    if (e.action == KeyAction::Long) return;
    switch (e.key) {
      case Key::Left: selectPanel(panelIndex_ - 1); break;
      case Key::Right: selectPanel(panelIndex_ + 1); break;
      case Key::Down:
      case Key::Select:
        if (e.action == KeyAction::Press && !visibleItems().empty()) {
          focusTabs_ = false;
          itemIndex_ = 0;
          scroll_ = 0;
        }
        break;
      case Key::Return:
        // Opened over the tracking screen: go back to it. (The root stays.)
        if (e.action == KeyAction::Press) pop();
        break;
      default: break;
    }
  }

  void listKey(const KeyEvent& e) {
    MenuPanel* p = panel();
    MenuItem* it = item();
    if (p == nullptr || it == nullptr) {
      focusTabs_ = true;
      return;
    }
    int n = static_cast<int>(visibleItems().size());
    switch (e.key) {
      case Key::Up:
        if (itemIndex_ == 0) {
          if (e.action == KeyAction::Press) focusTabs_ = true;
        } else {
          itemIndex_--;
        }
        break;
      case Key::Down:
        if (itemIndex_ < n - 1) itemIndex_++;
        break;
      case Key::Left:
      case Key::Right:
        if (it->adjustable()) adjust(*p, *it, e.key == Key::Right ? 1 : -1, 1);
        break;
      case Key::Select:
        if (e.action == KeyAction::Press) activate(*p, *it);
        break;
      case Key::Return:
        if (e.action == KeyAction::Press) focusTabs_ = true;
        break;
      default: break;
    }
  }

  void editKey(const KeyEvent& e) {
    MenuPanel* p = panel();
    MenuItem* it = item();
    if (p == nullptr || it == nullptr || it->kind != ItemKind::Number) {
      editing_ = false;
      return;
    }
    switch (e.key) {
      case Key::Left: adjust(*p, *it, -1, 1); break;
      case Key::Right: adjust(*p, *it, 1, 1); break;
      case Key::Down: adjust(*p, *it, -1, 10); break;
      case Key::Up: adjust(*p, *it, 1, 10); break;
      case Key::Select:
        if (e.action == KeyAction::Press) editing_ = false;
        break;
      case Key::Return:
        if (e.action == KeyAction::Press) {
          if (it->value != editOriginal_) {
            it->value = editOriginal_;
            commitItem(*p, *it);
          }
          editing_ = false;
        }
        break;
      default: break;
    }
  }

  bool usable(const MenuPanel& p, const MenuItem& it) {
    if (!remoteUsable(p)) {
      toast("The Pi is offline");
      return false;
    }
    if (!it.enabled) {
      toast(it.hint.length() > 0 ? it.hint : String("Not available"));
      return false;
    }
    return true;
  }

  void adjust(MenuPanel& p, MenuItem& it, int dir, int multiplier) {
    if (!usable(p, it)) return;
    int32_t before = it.value;
    switch (it.kind) {
      case ItemKind::Toggle:
        if (it.confirm.length() > 0) {
          activate(p, it);
          return;
        }
        it.value = it.value ? 0 : 1;
        break;
      case ItemKind::Choice: {
        int32_t n = static_cast<int32_t>(it.options.size());
        if (n == 0) return;
        it.value = std::max<int32_t>(0, std::min<int32_t>(n - 1, it.value + dir));
        break;
      }
      case ItemKind::Number: {
        int32_t v = it.value + dir * it.step * multiplier;
        it.value = std::max(it.minValue, std::min(it.maxValue, v));
        break;
      }
      default:
        return;
    }
    if (it.value != before) commitItem(p, it);
  }

  void activate(MenuPanel& p, MenuItem& it) {
    if (it.kind == ItemKind::Info) {
      String full = it.valueText();
      if (full.length() > 0) openMessage(it.label, full);
      return;
    }
    if (!usable(p, it)) return;
    String panelId = p.id;
    String itemId = it.id;
    // Look the item up again in callbacks: Pi menus may change meanwhile.
    auto again = [panelId, itemId](std::function<void(MenuPanel&, MenuItem&)> fn) {
      MenuPanel* pp = findPanel(panelId.c_str());
      MenuItem* ii = pp != nullptr ? pp->find(itemId.c_str()) : nullptr;
      if (ii != nullptr) fn(*pp, *ii);
    };
    switch (it.kind) {
      case ItemKind::Action:
        if (it.confirm.length() > 0) {
          openConfirm(it.label, it.confirm, [again](bool yes) {
            if (yes) again([](MenuPanel& pp, MenuItem& ii) { commitItem(pp, ii); });
          });
        } else {
          commitItem(p, it);
        }
        break;
      case ItemKind::Toggle:
        if (it.confirm.length() > 0) {
          openConfirm(it.label, it.confirm, [again](bool yes) {
            if (!yes) return;
            again([](MenuPanel& pp, MenuItem& ii) {
              ii.value = ii.value ? 0 : 1;
              commitItem(pp, ii);
            });
          });
        } else {
          it.value = it.value ? 0 : 1;
          commitItem(p, it);
        }
        break;
      case ItemKind::Choice:
        openChoiceList(it.label, it.options, it.value, [again](int index) {
          again([index](MenuPanel& pp, MenuItem& ii) {
            if (ii.value != index) {
              ii.value = index;
              commitItem(pp, ii);
            }
          });
        });
        break;
      case ItemKind::Number:
        editing_ = true;
        editOriginal_ = it.value;
        break;
      case ItemKind::Text:
        openKeyboard(it.label, it.text, it.maxLength, [again](const String& value) {
          again([value](MenuPanel& pp, MenuItem& ii) {
            ii.text = value;
            commitItem(pp, ii);
          });
        });
        break;
      case ItemKind::Screen:
        if (it.changed) it.changed(it);
        if (it.screen != nullptr) push(it.screen);
        break;
      default:
        break;
    }
  }

  // ---- drawing

  void drawTabs(Gfx& g) {
    const Theme& t = theme();
    int32_t w = screenW();
    g.fillRect(0, 0, w, kBarH, t.bar);
    g.setFont(fontBody());
    g.setTextDatum(textdatum_t::middle_center);

    // Tab extents in "strip" coordinates.
    std::vector<int32_t> xs, ws;
    int32_t x = 0;
    for (MenuPanel* p : g_panels) {
      int32_t tw = g.textWidth(p->title) + 10;
      xs.push_back(x);
      ws.push_back(tw);
      x += tw + 2;
    }
    int32_t total = x;
    // Keep the selected tab in view, with a little of its neighbours.
    int32_t margin = 8;
    int32_t selX = xs[panelIndex_];
    int32_t selW = ws[panelIndex_];
    if (selX - tabScroll_ < margin) tabScroll_ = selX - margin;
    if (selX + selW - tabScroll_ > w - margin) tabScroll_ = selX + selW - (w - margin);
    if (tabScroll_ > total - w) tabScroll_ = total - w;
    if (tabScroll_ < 0) tabScroll_ = 0;

    for (size_t i = 0; i < g_panels.size(); i++) {
      int32_t tx = xs[i] - tabScroll_;
      if (tx + ws[i] < 0 || tx > w) continue;
      bool selected = static_cast<int>(i) == panelIndex_;
      bool offline = !remoteUsable(*g_panels[i]);
      uint16_t color = offline ? t.textDim : t.text;
      if (selected && focusTabs_) {
        g.fillRoundRect(tx, 2, ws[i], kBarH - 4, 3, t.accent);
        color = t.onAccent;
      } else if (selected) {
        g.fillRect(tx + 2, kBarH - 3, ws[i] - 4, 2, t.accent);
        color = t.accent;
      } else if (!offline) {
        color = t.textDim;
      }
      g.setTextColor(color);
      g.drawString(g_panels[i]->title, tx + ws[i] / 2, kBarH / 2);
    }
    if (tabScroll_ > 0) {
      g.fillRect(0, 0, 7, kBarH, t.bar);
      drawArrow(g, 3, kBarH / 2, 3, 'l', t.textDim);
    }
    if (total - tabScroll_ > w) {
      g.fillRect(w - 7, 0, 7, kBarH, t.bar);
      drawArrow(g, w - 4, kBarH / 2, 3, 'r', t.textDim);
    }
  }

  void drawList(Gfx& g) {
    const Theme& t = theme();
    MenuPanel* p = panel();
    int32_t w = screenW();
    int32_t top = kBarH + 2;
    int32_t bottom = screenH() - kFooterH;
    g.setFont(fontBody());
    if (p == nullptr) {
      g.setTextDatum(textdatum_t::middle_center);
      g.setTextColor(t.textDim);
      g.drawString("No panels", w / 2, (top + bottom) / 2);
      return;
    }
    if (!remoteUsable(*p)) {
      g.fillRect(0, top - 2, w, 12, t.surface);
      g.setTextDatum(textdatum_t::middle_center);
      g.setTextColor(t.warn);
      g.drawString(link() == Link::Waiting ? "Waiting for the Pi" : "Pi offline", w / 2, top + 4);
      top += 11;
    }
    // Refresh every item first: a refresh may show or hide items.
    for (MenuItem& it : p->items) {
      if (it.refresh) it.refresh(it);
    }
    std::vector<int> vis = visibleItems();
    if (vis.empty()) {
      g.setTextDatum(textdatum_t::middle_center);
      g.setTextColor(t.textDim);
      g.drawString("Nothing here yet", w / 2, (top + bottom) / 2);
      return;
    }
    int rows = (bottom - top) / kRowH;
    if (rows < 1) rows = 1;
    if (itemIndex_ < scroll_) scroll_ = itemIndex_;
    if (itemIndex_ >= scroll_ + rows) scroll_ = itemIndex_ - rows + 1;
    if (scroll_ > static_cast<int>(vis.size()) - rows) scroll_ = std::max(0, static_cast<int>(vis.size()) - rows);

    bool offline = !remoteUsable(*p);
    int32_t rightEdge = static_cast<int32_t>(vis.size()) > rows ? w - 5 : w - 2;
    for (int r = 0; r < rows && scroll_ + r < static_cast<int>(vis.size()); r++) {
      int k = scroll_ + r;
      MenuItem& it = p->items[vis[k]];
      int32_t y = top + r * kRowH;
      int32_t cy = y + kRowH / 2;
      bool selected = !focusTabs_ && k == itemIndex_;
      if (selected) {
        g.fillRect(0, y, rightEdge, kRowH, t.select);
        g.fillRect(0, y, 2, kRowH, t.accent);
      }
      bool dim = offline || !it.enabled;
      drawRow(g, it, y, cy, rightEdge, selected, dim);
    }
    if (static_cast<int32_t>(vis.size()) > rows) {
      int32_t trackH = bottom - top;
      int32_t thumbH = std::max<int32_t>(6, trackH * rows / static_cast<int32_t>(vis.size()));
      int32_t thumbY = top + (trackH - thumbH) * scroll_ / std::max(1, static_cast<int>(vis.size()) - rows);
      g.fillRect(w - 3, top, 2, trackH, t.surface);
      g.fillRect(w - 3, thumbY, 2, thumbH, t.textDim);
    }
  }

  void drawRow(Gfx& g, MenuItem& it, int32_t y, int32_t cy, int32_t right, bool selected, bool dim) {
    const Theme& t = theme();
    int32_t valueLeft = right - 4;
    g.setTextDatum(textdatum_t::middle_right);
    switch (it.kind) {
      case ItemKind::Toggle:
        drawToggle(g, right - 22, cy, it.value != 0, selected);
        valueLeft = right - 24;
        break;
      case ItemKind::Action:
      case ItemKind::Screen:
        drawArrow(g, right - 6, cy, 3, 'r', selected ? t.accent : t.textDim);
        valueLeft = right - 12;
        break;
      default: {
        String v = it.valueText();
        uint16_t color = t.textDim;
        if (it.kind == ItemKind::Text && it.text.length() == 0) {
          v = "not set";
          color = t.warn;
        } else if (selected) {
          color = t.accent;
        }
        bool arrows = selected && (it.kind == ItemKind::Choice || it.kind == ItemKind::Number);
        // The value wins space over the label, but the label keeps 40 %.
        int32_t labelW = std::min<int32_t>(g.textWidth(it.label), (right - 6) * 2 / 5);
        int32_t maxValueW = right - 6 - labelW - 8 - (arrows ? 14 : 0);
        int32_t vx = right - 4;
        if (arrows) {
          drawArrow(g, right - 5, cy, 3, 'r', t.accent);
          vx = right - 11;
        }
        String shown = fitText(g, v, maxValueW);
        int32_t vw = g.textWidth(shown);
        if (editing_ && selected) {
          g.fillRoundRect(vx - vw - 3, y + 1, vw + 6, kRowH - 2, 2, t.accent);
          color = t.onAccent;
        }
        g.setTextColor(color);
        g.drawString(shown, vx, cy);
        if (arrows) drawArrow(g, vx - vw - 6, cy, 3, 'l', t.accent);
        valueLeft = vx - vw - (arrows ? 12 : 6);
        break;
      }
    }
    g.setTextDatum(textdatum_t::middle_left);
    g.setTextColor(dim ? t.textDim : t.text);
    g.drawString(fitText(g, it.label, valueLeft - 6), 6, cy);
  }
};

MenuScreen g_menu;

}  // namespace

// ------------------------------------------------------------ MenuItem

String MenuItem::valueText() const {
  switch (kind) {
    case ItemKind::Toggle:
      return value ? "On" : "Off";
    case ItemKind::Choice:
      return value >= 0 && value < static_cast<int32_t>(options.size()) ? options[value] : String("?");
    case ItemKind::Number: {
      String s;
      if (decimals == 0) {
        s = String(value);
      } else {
        s = String(static_cast<double>(value) / pow10i(decimals), static_cast<unsigned int>(decimals));
      }
      if (unit.length() > 0) {
        s += unit.startsWith("%") ? "" : " ";
        s += unit;
      }
      return s;
    }
    case ItemKind::Text:
      if (secret && text.length() > 0) {
        String masked;
        size_t n = text.length();
        for (size_t i = 0; i < std::min<size_t>(n > 4 ? n - 4 : 0, 6); i++) masked += '*';
        masked += n > 4 ? text.substring(n - 4) : String("****");
        return masked;
      }
      return text;
    case ItemKind::Info:
      return text;
    default:
      return "";
  }
}

String MenuScreen::footerHint() const {
  if (focusTabs_) return "\x11\x10 panel \x07 open";
  if (editing_) return "\x1e\x1f\x11\x10 \x07 ok";
  const MenuItem* it = item();
  if (it == nullptr) return "";
  if (it->hint.length() > 0 && it->hint.length() <= 12) return it->hint;
  switch (it->kind) {
    case ItemKind::Action: return "\x07 run";
    case ItemKind::Toggle: return "\x07 switch";
    case ItemKind::Choice: return "\x11\x10 change";
    case ItemKind::Number: return "\x11\x10 \x07 edit";
    case ItemKind::Text: return "\x07 edit";
    case ItemKind::Screen: return "\x07 open";
    default: return "";
  }
}

const char* itemKindName(ItemKind kind) {
  switch (kind) {
    case ItemKind::Action: return "action";
    case ItemKind::Toggle: return "toggle";
    case ItemKind::Choice: return "choice";
    case ItemKind::Number: return "number";
    case ItemKind::Text: return "text";
    case ItemKind::Info: return "info";
    case ItemKind::Screen: return "screen";
  }
  return "";
}

bool parseItemKind(const char* name, ItemKind& out) {
  for (int i = 0; i <= static_cast<int>(ItemKind::Screen); i++) {
    ItemKind k = static_cast<ItemKind>(i);
    if (strcmp(itemKindName(k), name) == 0) {
      out = k;
      return true;
    }
  }
  return false;
}

MenuItem* MenuPanel::find(const char* itemId) {
  for (MenuItem& it : items) {
    if (it.id == itemId) return &it;
  }
  return nullptr;
}

// ------------------------------------------------------------ builder

MenuItem& PanelBuilder::add(ItemKind kind, const char* id, const char* label) {
  panel_.items.emplace_back();
  MenuItem& it = panel_.items.back();
  it.kind = kind;
  it.id = id;
  it.label = label;
  return it;
}

MenuItem& PanelBuilder::action(const char* id, const char* label, std::function<void()> run) {
  MenuItem& it = add(ItemKind::Action, id, label);
  it.changed = [run](MenuItem&) { run(); };
  return it;
}

MenuItem& PanelBuilder::toggle(const char* id, const char* label, std::function<bool()> get,
                               std::function<void(bool)> set) {
  MenuItem& it = add(ItemKind::Toggle, id, label);
  it.refresh = [get](MenuItem& i) { i.value = get() ? 1 : 0; };
  it.changed = [set](MenuItem& i) { set(i.value != 0); };
  return it;
}

MenuItem& PanelBuilder::choice(const char* id, const char* label, std::initializer_list<const char*> options,
                               std::function<int()> get, std::function<void(int)> set) {
  MenuItem& it = add(ItemKind::Choice, id, label);
  for (const char* o : options) it.options.push_back(o);
  it.refresh = [get](MenuItem& i) { i.value = get(); };
  it.changed = [set](MenuItem& i) { set(i.value); };
  return it;
}

MenuItem& PanelBuilder::number(const char* id, const char* label, int32_t minValue, int32_t maxValue, int32_t step,
                               const char* unit, std::function<int32_t()> get, std::function<void(int32_t)> set) {
  MenuItem& it = add(ItemKind::Number, id, label);
  it.minValue = minValue;
  it.maxValue = maxValue;
  it.step = step;
  it.unit = unit != nullptr ? unit : "";
  it.refresh = [get](MenuItem& i) { i.value = get(); };
  it.changed = [set](MenuItem& i) { set(i.value); };
  return it;
}

MenuItem& PanelBuilder::text(const char* id, const char* label, uint16_t maxLength, std::function<String()> get,
                             std::function<void(const String&)> set) {
  MenuItem& it = add(ItemKind::Text, id, label);
  it.maxLength = maxLength;
  it.refresh = [get](MenuItem& i) { i.text = get(); };
  it.changed = [set](MenuItem& i) { set(i.text); };
  return it;
}

MenuItem& PanelBuilder::info(const char* id, const char* label, std::function<String()> get) {
  MenuItem& it = add(ItemKind::Info, id, label);
  it.refresh = [get](MenuItem& i) { i.text = get(); };
  return it;
}

MenuItem& PanelBuilder::screen(const char* id, const char* label, ui::Screen* s) {
  MenuItem& it = add(ItemKind::Screen, id, label);
  it.screen = s;
  return it;
}

PanelBuilder addPanel(const char* id, const char* title, int order) {
  MenuPanel* p = new MenuPanel();
  p->id = id;
  p->title = title;
  p->order = order;
  g_panels.push_back(p);
  sortPanels();
  if (g_selectedPanelId.length() == 0) g_selectedPanelId = g_panels.front()->id;
  return PanelBuilder(*p);
}

// ------------------------------------------------------------ Pi panels

bool setRemotePanels(JsonArrayConst panels, String* error) {
  std::vector<MenuPanel*> fresh;
  for (JsonObjectConst o : panels) {
    const char* id = o["id"] | "";
    if (id[0] == '\0') {
      if (error) *error = "panel without id";
      for (MenuPanel* p : fresh) delete p;
      return false;
    }
    MenuPanel* local = findPanel(id);
    if (local != nullptr && !local->remote) {
      if (error) *error = String("panel id taken by the firmware: ") + id;
      for (MenuPanel* p : fresh) delete p;
      return false;
    }
    MenuPanel* p = new MenuPanel();
    p->id = id;
    p->title = o["title"] | id;
    p->order = o["order"] | 50;
    p->remote = true;
    for (JsonObjectConst io : o["items"].as<JsonArrayConst>()) {
      MenuItem it;
      if (!parseRemoteItem(io, it, error)) {
        delete p;
        for (MenuPanel* q : fresh) delete q;
        return false;
      }
      p->items.push_back(std::move(it));
    }
    fresh.push_back(p);
  }
  // Swap: drop the old Pi panels, keep the firmware ones.
  std::vector<MenuPanel*> kept;
  for (MenuPanel* p : g_panels) {
    if (p->remote) {
      delete p;
    } else {
      kept.push_back(p);
    }
  }
  g_panels = kept;
  for (MenuPanel* p : fresh) g_panels.push_back(p);
  sortPanels();
  if (findPanel(g_selectedPanelId.c_str()) == nullptr && !g_panels.empty()) {
    g_selectedPanelId = g_panels.front()->id;
  }
  invalidate();
  return true;
}

int setRemoteValues(JsonObjectConst values) {
  int changed = 0;
  for (JsonPairConst kv : values) {
    String key = kv.key().c_str();
    int dot = key.indexOf('.');
    if (dot <= 0) continue;
    MenuPanel* p = findPanel(key.substring(0, dot).c_str());
    if (p == nullptr || !p->remote) continue;
    MenuItem* it = p->find(key.substring(dot + 1).c_str());
    if (it == nullptr) continue;
    applyJsonValue(*it, kv.value());
    changed++;
  }
  if (changed > 0) invalidate();
  return changed;
}

bool updateRemoteItem(const char* panelId, const char* itemId, JsonObjectConst fields) {
  MenuPanel* p = findPanel(panelId);
  if (p == nullptr || !p->remote) return false;
  MenuItem* it = p->find(itemId);
  if (it == nullptr) return false;
  applyJsonFields(*it, fields);
  invalidate();
  return true;
}

MenuPanel* findPanel(const char* id) {
  for (MenuPanel* p : g_panels) {
    if (p->id == id) return p;
  }
  return nullptr;
}

int panelCount() {
  return static_cast<int>(g_panels.size());
}

MenuPanel& panelAt(int index) {
  return *g_panels[index];
}

void commitItem(MenuPanel& panel, MenuItem& item) {
  if (panel.remote) {
    emitChange(panel, item);
  } else {
    if (item.changed) item.changed(item);
    if (item.refresh) item.refresh(item);
  }
  invalidate();
}

bool menuShowPanel(const char* panelId, const char* itemId) {
  if (findPanel(panelId) == nullptr) return false;
  showMenu();
  g_menu.showPanel(panelId, itemId);
  return true;
}

void menuStateJson(JsonObject out) {
  g_menu.stateJson(out);
  JsonArray panels = out["panels"].to<JsonArray>();
  for (MenuPanel* p : g_panels) {
    JsonObject po = panels.add<JsonObject>();
    po["id"] = p->id;
    po["title"] = p->title;
    po["remote"] = p->remote;
    po["items"] = p->items.size();
  }
}

Screen* menuScreen() {
  return &g_menu;
}

void menuResetFocus() {
  g_menu.resetFocus();
}

}  // namespace ui
