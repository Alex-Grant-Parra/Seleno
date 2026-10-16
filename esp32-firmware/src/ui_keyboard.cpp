// On-screen keyboard: a QWERTY grid plus a row of special keys, driven by
// the direction pad. SELECT types the highlighted key, holding SELECT
// finishes, RETURN cancels.
#include <algorithm>

#include "ui_dialogs.h"
#include "ui_internal.h"

namespace ui {

namespace {

enum class Special : uint8_t { None, Shift, Symbols, CursorLeft, Space, CursorRight, Delete, Done };

// Widths in quarter keys; every row is 40 quarters wide.
constexpr int kRowUnits = 40;
constexpr int kCharUnits = 4;

struct SpecialKey {
  Special kind;
  uint8_t units;
};
constexpr SpecialKey kBottomRow[] = {
    {Special::Shift, 6},      {Special::Symbols, 6}, {Special::CursorLeft, 4}, {Special::Space, 10},
    {Special::CursorRight, 4}, {Special::Delete, 5}, {Special::Done, 5},
};
constexpr int kBottomCount = sizeof(kBottomRow) / sizeof(kBottomRow[0]);

enum class Layer : uint8_t { Lower, Upper, Symbols };

const char* const kLayers[3][4] = {
    {"1234567890", "qwertyuiop", "asdfghjkl-", "zxcvbnm._@"},
    {"1234567890", "QWERTYUIOP", "ASDFGHJKL-", "ZXCVBNM._@"},
    {"!@#$%^&*()", "-_=+[]{}\\|", ";:'\",.<>/?", "`~"},
};
constexpr int kCharRows = 4;
constexpr int kRows = kCharRows + 1;  // + the special row

class KeyboardScreen : public Screen {
 public:
  void open(const String& title, const String& value, uint16_t maxLength, std::function<void(const String&)> done,
            std::function<void()> cancelled, const String& subtitle) {
    title_ = title;
    subtitle_ = subtitle;
    text_ = value;
    original_ = value;
    maxLength_ = maxLength == 0 ? 64 : maxLength;
    if (text_.length() > maxLength_) text_ = text_.substring(0, maxLength_);
    cursor_ = text_.length();
    done_ = std::move(done);
    cancelled_ = std::move(cancelled);
    layer_ = Layer::Lower;
    capsLock_ = false;
    row_ = 1;
    col_ = 0;
    prefX_ = keyCenter(row_, col_);
    push(this);
  }

  const char* name() const override { return "keyboard"; }
  uint32_t refreshMs() const override { return 500; }  // cursor blink
  String footerHint() const override { return "\x07key hold\x07=OK"; }

  void stateJson(JsonObject out) const override {
    out["title"] = title_;
    out["text"] = text_;
    out["cursor"] = cursor_;
    out["row"] = row_;
    out["col"] = col_;
    out["layer"] = layer_ == Layer::Lower ? "lower" : (layer_ == Layer::Upper ? "upper" : "symbols");
    out["caps_lock"] = capsLock_;
    out["key"] = keyLabel(row_, col_);
  }

  void key(const KeyEvent& e) override {
    switch (e.key) {
      case Key::Left: moveCol(-1); break;
      case Key::Right: moveCol(1); break;
      case Key::Up: moveRow(-1); break;
      case Key::Down: moveRow(1); break;
      case Key::Select:
        if (e.action == KeyAction::Long) {
          finish();
        } else if (e.action == KeyAction::Press) {
          press();
        }
        break;
      case Key::Return:
        if (e.action == KeyAction::Press) cancel();
        break;
      default: break;
    }
  }

  void draw(Gfx& g) override {
    const Theme& t = theme();
    int32_t w = screenW();
    char count[12];
    snprintf(count, sizeof(count), "%u/%u", static_cast<unsigned>(text_.length()), static_cast<unsigned>(maxLength_));
    drawTitleBar(g, title_, count);

    // Text box, scrolled so the cursor shows.
    int32_t boxY = kBarH + 3;
    int32_t boxH = 18;
    g.fillRoundRect(2, boxY, w - 4, boxH, 3, t.surface);
    g.setFont(fontLarge());
    g.setTextDatum(textdatum_t::middle_left);
    int32_t inner = w - 12;
    String before = text_.substring(0, cursor_);
    int32_t cursorPx = g.textWidth(before);
    if (cursorPx - scrollPx_ > inner) scrollPx_ = cursorPx - inner;
    if (cursorPx - scrollPx_ < 0) scrollPx_ = cursorPx;
    int32_t fullPx = g.textWidth(text_);
    if (scrollPx_ > 0 && fullPx - scrollPx_ < inner) scrollPx_ = std::max<int32_t>(0, fullPx - inner);
    g.setClipRect(5, boxY, inner + 2, boxH);
    if (text_.length() == 0) {
      g.setTextColor(t.textDim);
      g.setFont(fontBody());
      g.drawString("empty", 8, boxY + boxH / 2);
      g.setFont(fontLarge());
    } else {
      g.setTextColor(t.text);
      g.drawString(text_, 6 - scrollPx_, boxY + boxH / 2);
    }
    if ((millis() / 500) % 2 == 0) {
      g.fillRect(6 - scrollPx_ + cursorPx, boxY + 3, 1, boxH - 6, t.accent);
    }
    g.clearClipRect();

    int32_t gridTop = boxY + boxH + 3;
    if (subtitle_.length() > 0 && screenH() >= 150) {
      g.setFont(fontSmall());
      g.setTextDatum(textdatum_t::top_left);
      g.setTextColor(t.textDim);
      g.drawString(fitText(g, subtitle_, w - 6), 3, gridTop);
      gridTop += 10;
    }
    int32_t avail = screenH() - kFooterH - gridTop - 1;
    int32_t rowH = std::min<int32_t>(18, avail / kRows);
    int32_t unitW10 = (w - 4) * 10 / kRowUnits;  // pixels per quarter key, x10

    for (int r = 0; r < kRows; r++) {
      int n = rowLength(r);
      int32_t startUnits = 0;
      for (int c = 0; c < n; c++) {
        int units = keyUnits(r, c);
        int32_t x0 = 2 + startUnits * unitW10 / 10;
        int32_t x1 = 2 + (startUnits + units) * unitW10 / 10;
        int32_t y0 = gridTop + r * rowH;
        bool sel = r == row_ && c == col_;
        uint16_t bg = sel ? t.accent : t.surface;
        uint16_t fg = sel ? t.onAccent : t.text;
        if (!sel && r == kCharRows && kBottomRow[c].kind == Special::Done) {
          fg = t.accent;
        }
        g.fillRoundRect(x0 + 1, y0 + 1, x1 - x0 - 1, rowH - 2, 2, bg);
        drawKeyFace(g, r, c, (x0 + x1) / 2 + 1, y0 + rowH / 2, fg);
        startUnits += units;
      }
    }
  }

  void dismiss() {
    done_ = nullptr;
    cancelled_ = nullptr;
    if (onStack(this)) {
      while (top() != this) pop();
      pop();
    }
  }

 private:
  String title_;
  String subtitle_;
  String text_;
  String original_;
  uint16_t maxLength_ = 64;
  unsigned cursor_ = 0;
  int32_t scrollPx_ = 0;
  std::function<void(const String&)> done_;
  std::function<void()> cancelled_;
  Layer layer_ = Layer::Lower;
  bool capsLock_ = false;
  int row_ = 1;
  int col_ = 0;
  int prefX_ = 0;  // remembered column (in quarter keys) for up/down

  const char* rowChars(int r) const { return kLayers[static_cast<int>(layer_)][r]; }

  int rowLength(int r) const {
    return r == kCharRows ? kBottomCount : static_cast<int>(strlen(rowChars(r)));
  }

  int keyUnits(int r, int c) const { return r == kCharRows ? kBottomRow[c].units : kCharUnits; }

  int keyCenter(int r, int c) const {
    int x = 0;
    for (int i = 0; i < c; i++) x += keyUnits(r, i);
    return x + keyUnits(r, c) / 2;
  }

  String keyLabel(int r, int c) const {
    if (r < kCharRows) return String(rowChars(r)[c]);
    switch (kBottomRow[c].kind) {
      case Special::Shift: return "shift";
      case Special::Symbols: return "symbols";
      case Special::CursorLeft: return "cursor_left";
      case Special::Space: return "space";
      case Special::CursorRight: return "cursor_right";
      case Special::Delete: return "delete";
      case Special::Done: return "done";
      default: return "";
    }
  }

  void drawKeyFace(Gfx& g, int r, int c, int32_t cx, int32_t cy, uint16_t fg) {
    g.setTextColor(fg);
    g.setTextDatum(textdatum_t::middle_center);
    if (r < kCharRows) {
      g.setFont(fontBody());
      char s[2] = {rowChars(r)[c], 0};
      g.drawString(s, cx, cy);
      return;
    }
    g.setFont(fontSmall());
    switch (kBottomRow[c].kind) {
      case Special::Shift:
        g.drawString(layer_ == Layer::Upper ? (capsLock_ ? "AB" : "Ab") : "ab", cx, cy);
        if (capsLock_) g.drawFastHLine(cx - 6, cy + 5, 12, fg);
        break;
      case Special::Symbols: g.drawString(layer_ == Layer::Symbols ? "abc" : "#+", cx, cy); break;
      case Special::CursorLeft: drawArrow(g, cx, cy, 3, 'l', fg); break;
      case Special::CursorRight: drawArrow(g, cx, cy, 3, 'r', fg); break;
      case Special::Space:
        g.drawFastHLine(cx - 8, cy + 2, 17, fg);
        g.drawFastVLine(cx - 8, cy - 1, 3, fg);
        g.drawFastVLine(cx + 8, cy - 1, 3, fg);
        break;
      case Special::Delete:
        drawArrow(g, cx - 4, cy, 3, 'l', fg);
        g.fillRect(cx - 3, cy - 1, 7, 3, fg);
        break;
      case Special::Done: g.drawString("OK", cx, cy); break;
      default: break;
    }
  }

  void moveCol(int dir) {
    int n = rowLength(row_);
    col_ = ((col_ + dir) % n + n) % n;
    prefX_ = keyCenter(row_, col_);
  }

  void moveRow(int dir) {
    row_ = ((row_ + dir) % kRows + kRows) % kRows;
    // The key in the new row nearest the remembered column.
    int best = 0;
    int bestDist = 1000;
    for (int c = 0; c < rowLength(row_); c++) {
      int d = abs(keyCenter(row_, c) - prefX_);
      if (d < bestDist) {
        bestDist = d;
        best = c;
      }
    }
    col_ = best;
  }

  void setLayer(Layer layer) {
    layer_ = layer;
    for (int r = 0; r < kCharRows; r++) {
      if (row_ == r && col_ >= rowLength(r)) col_ = rowLength(r) - 1;
    }
  }

  void insert(char ch) {
    if (text_.length() >= maxLength_) {
      toast("That's the maximum length", 1000);
      return;
    }
    text_ = text_.substring(0, cursor_) + ch + text_.substring(cursor_);
    cursor_++;
    if (layer_ == Layer::Upper && !capsLock_) setLayer(Layer::Lower);
  }

  void press() {
    if (row_ < kCharRows) {
      insert(rowChars(row_)[col_]);
      return;
    }
    switch (kBottomRow[col_].kind) {
      case Special::Shift:
        if (layer_ != Layer::Upper) {
          setLayer(Layer::Upper);
          capsLock_ = false;
        } else if (!capsLock_) {
          capsLock_ = true;
        } else {
          capsLock_ = false;
          setLayer(Layer::Lower);
        }
        break;
      case Special::Symbols:
        setLayer(layer_ == Layer::Symbols ? Layer::Lower : Layer::Symbols);
        capsLock_ = false;
        break;
      case Special::CursorLeft:
        if (cursor_ > 0) cursor_--;
        break;
      case Special::CursorRight:
        if (cursor_ < text_.length()) cursor_++;
        break;
      case Special::Space: insert(' '); break;
      case Special::Delete:
        if (cursor_ > 0) {
          text_.remove(cursor_ - 1, 1);
          cursor_--;
        }
        break;
      case Special::Done: finish(); break;
      default: break;
    }
  }

  void finish() {
    auto done = std::move(done_);
    String value = text_;
    done_ = nullptr;
    cancelled_ = nullptr;
    pop();
    if (done) done(value);
  }

  void cancel() {
    if (text_ != original_) {
      openConfirm("Discard?", "Throw away what you typed?", [this](bool yes) {
        if (yes && top() == this) closeCancelled();
      }, "Discard", "Keep");
      return;
    }
    closeCancelled();
  }

  void closeCancelled() {
    auto cancelled = std::move(cancelled_);
    done_ = nullptr;
    cancelled_ = nullptr;
    pop();
    if (cancelled) cancelled();
  }
};

KeyboardScreen g_keyboard;

}  // namespace

void openKeyboard(const String& title, const String& value, uint16_t maxLength,
                  std::function<void(const String&)> done, std::function<void()> cancelled, const String& subtitle) {
  if (onStack(&g_keyboard)) {
    g_keyboard.dismiss();
  }
  g_keyboard.open(title, value, maxLength, std::move(done), std::move(cancelled), subtitle);
}

Screen* keyboardScreen() {
  return &g_keyboard;
}

void dismissKeyboard() {
  g_keyboard.dismiss();
}

}  // namespace ui
