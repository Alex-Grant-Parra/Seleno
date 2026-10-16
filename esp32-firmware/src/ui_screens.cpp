// Root screens (loading, tracking) and the shared dialogs.
#include "ui_screens.h"

#include <cmath>

#include "config.h"
#include "estop.h"
#include "motors.h"
#include "stepper.h"
#include "ui_dialogs.h"
#include "ui_menu.h"
#include "ui_sky.h"

namespace ui {

namespace {

// ------------------------------------------------------------ loading

class LoadingScreen : public Screen {
 public:
  const char* name() const override { return "loading"; }
  uint32_t refreshMs() const override { return 80; }
  bool returnNavigates() const override { return false; }
  bool wantsFooter() const override { return false; }

  void enter() override { since_ = millis(); }

  void key(const KeyEvent& e) override {
    // The board can be tested without a Pi: hold SELECT for the menu.
    if (e.key == Key::Select && e.action == KeyAction::Long) {
      offline_ = true;
      setRoot(menuScreen());
      toast("Offline menu: Pi panels appear when it connects", 2500);
    }
  }

  void draw(Gfx& g) override {
    const Theme& t = theme();
    int32_t w = screenW();
    int32_t h = screenH();
    uint32_t elapsed = millis() - since_;
    float phase = static_cast<float>(elapsed % 4000) / 4000.0f;

    int32_t logoY = h / 2 + 4;
    drawMoon(g, w / 2, logoY - 38, 15, phase);

    g.setFont(fontLogo());
    g.setTextDatum(textdatum_t::middle_center);
    g.setTextColor(t.text);
    g.drawString("SELENO", w / 2, logoY);

    g.setFont(fontBody());
    g.setTextColor(t.textDim);
    String status = loadingText.length() > 0 ? loadingText : String(elapsed < 2500 ? "Starting" : "Waiting for the Pi");
    int dots = (elapsed / 400) % 4;
    String shown = status;
    for (int i = 0; i < dots; i++) shown += '.';
    // Keep the text still while the dots change.
    int32_t tw = g.textWidth(status);
    g.setTextDatum(textdatum_t::middle_left);
    g.drawString(shown, (w - tw) / 2, logoY + 26);

    if (elapsed > 12000) {
      String hint = "hold \x07 for the menu";
      drawIconText(g, hint, (w - iconTextWidth(g, hint)) / 2, h - 20, t.textDim);
    }
    g.setFont(fontSmall());
    g.setTextDatum(textdatum_t::bottom_center);
    g.setTextColor(t.select);
    g.drawString("v" SELENO_FW_VERSION, w / 2, h - 2);
  }

  String loadingText;
  bool offline_ = false;

 private:
  uint32_t since_ = 0;
};

LoadingScreen g_loading;

bool g_trackingPending = false;

// ------------------------------------------------------------ confirm

class ConfirmScreen : public Screen {
 public:
  void open(const String& title, const String& text, std::function<void(bool)> result, const char* yes,
            const char* no, bool defaultYes) {
    title_ = title;
    text_ = text;
    yes_ = yes != nullptr ? yes : "Yes";
    no_ = no != nullptr ? no : "No";
    result_ = std::move(result);
    yesSelected_ = defaultYes;  // normally the safe answer
    push(this);
  }

  const char* name() const override { return "confirm"; }
  String footerHint() const override { return "\x11\x10 \x07 ok"; }

  void stateJson(JsonObject out) const override {
    out["title"] = title_;
    out["text"] = text_;
    out["selected"] = yesSelected_ ? yes_ : no_;
  }

  void key(const KeyEvent& e) override {
    if (e.action == KeyAction::Long) return;
    switch (e.key) {
      case Key::Left:
      case Key::Right:
      case Key::Up:
      case Key::Down: yesSelected_ = !yesSelected_; break;
      case Key::Select: close(yesSelected_); break;
      case Key::Return: close(false); break;
      default: break;
    }
  }

  void draw(Gfx& g) override {
    const Theme& t = theme();
    int32_t w = screenW();
    drawTitleBar(g, title_);
    g.setFont(fontBody());
    drawWrapped(g, text_, 6, kBarH + 8, w - 12, t.text, 12);
    int32_t by = screenH() - kFooterH - 24;
    int32_t bw = (w - 18) / 2;
    button(g, 6, by, bw, no_, !yesSelected_);
    button(g, 12 + bw, by, bw, yes_, yesSelected_);
  }

  void dismiss() {
    result_ = nullptr;
    if (onStack(this)) {
      popTo(this);
      pop();
    }
  }

 private:
  String title_, text_, yes_, no_;
  std::function<void(bool)> result_;
  bool yesSelected_ = false;

  void button(Gfx& g, int32_t x, int32_t y, int32_t w, const String& label, bool selected) {
    const Theme& t = theme();
    g.fillRoundRect(x, y, w, 18, 4, selected ? t.accent : t.surface);
    g.setFont(fontBody());
    g.setTextDatum(textdatum_t::middle_center);
    g.setTextColor(selected ? t.onAccent : t.text);
    g.drawString(fitText(g, label, w - 4), x + w / 2, y + 9);
  }

  void close(bool yes) {
    auto result = std::move(result_);
    result_ = nullptr;
    pop();
    if (result) result(yes);
  }
};

ConfirmScreen g_confirm;

// ------------------------------------------------------------ message

class MessageScreen : public Screen {
 public:
  void open(const String& title, const String& text, std::function<void()> closed, bool busy, uint32_t timeoutMs) {
    title_ = title;
    text_ = text;
    closed_ = std::move(closed);
    busy_ = busy;
    scroll_ = 0;
    closeAt_ = timeoutMs > 0 ? millis() + timeoutMs : 0;
    if (!onStack(this)) push(this);
    invalidate();
  }

  const char* name() const override { return "message"; }
  uint32_t refreshMs() const override { return busy_ || closeAt_ != 0 ? 100 : 0; }
  String footerHint() const override { return busy_ ? "" : "\x07 ok"; }

  void stateJson(JsonObject out) const override {
    out["title"] = title_;
    out["text"] = text_;
    out["busy"] = busy_;
  }

  void key(const KeyEvent& e) override {
    if (e.action == KeyAction::Long) return;
    if (e.key == Key::Down) scroll_ += 12;
    if (e.key == Key::Up && scroll_ > 0) scroll_ -= 12;
    if (e.key == Key::Return || (e.key == Key::Select && !busy_)) close();
  }

  void draw(Gfx& g) override {
    const Theme& t = theme();
    int32_t w = screenW();
    if (closeAt_ != 0 && static_cast<int32_t>(millis() - closeAt_) >= 0) {
      closeAt_ = 0;
      close();
      return;
    }
    drawTitleBar(g, title_);
    g.setFont(fontBody());
    int32_t top = kBarH + 6;
    int32_t bottom = screenH() - kFooterH - (busy_ ? 30 : 4);
    g.setClipRect(0, top, w, bottom - top);
    int32_t used = drawWrapped(g, text_, 6, top - scroll_, w - 12, t.text, 12);
    g.clearClipRect();
    if (used - scroll_ > bottom - top) drawArrow(g, w - 6, bottom - 4, 3, 'd', t.textDim);
    if (busy_) {
      // A ring of dots chasing round.
      int32_t cx = w / 2;
      int32_t cy = screenH() - kFooterH - 16;
      int step = (millis() / 110) % 8;
      for (int i = 0; i < 8; i++) {
        float a = i * 3.14159f / 4.0f;
        uint16_t c = i == step ? t.accent : t.surface;
        g.fillCircle(cx + static_cast<int32_t>(9 * cosf(a)), cy + static_cast<int32_t>(9 * sinf(a)), 2, c);
      }
    }
  }

  void dismiss() {
    closed_ = nullptr;
    if (onStack(this)) {
      popTo(this);
      pop();
    }
  }

 private:
  String title_, text_;
  std::function<void()> closed_;
  bool busy_ = false;
  int32_t scroll_ = 0;
  uint32_t closeAt_ = 0;

  void close() {
    auto closed = std::move(closed_);
    closed_ = nullptr;
    if (top() == this) pop();
    if (closed) closed();
  }
};

MessageScreen g_message;

// ------------------------------------------------------------ choice list

class ChoiceScreen : public Screen {
 public:
  void open(const String& title, const std::vector<String>& options, int selected, std::function<void(int)> done,
            std::function<void()> cancelled) {
    title_ = title;
    options_ = options;
    index_ = std::max(0, std::min(selected, static_cast<int>(options.size()) - 1));
    current_ = index_;
    done_ = std::move(done);
    cancelled_ = std::move(cancelled);
    scroll_ = 0;
    push(this);
  }

  const char* name() const override { return "choice"; }
  String footerHint() const override { return "\x1e\x1f \x07 pick"; }

  void stateJson(JsonObject out) const override {
    out["title"] = title_;
    out["index"] = index_;
    out["option"] = index_ < static_cast<int>(options_.size()) ? options_[index_] : String("");
  }

  void key(const KeyEvent& e) override {
    int n = static_cast<int>(options_.size());
    if (n == 0) {
      close(-1);
      return;
    }
    switch (e.key) {
      case Key::Up: index_ = (index_ + n - 1) % n; break;
      case Key::Down: index_ = (index_ + 1) % n; break;
      case Key::Select:
        if (e.action == KeyAction::Press) close(index_);
        break;
      case Key::Return:
        if (e.action == KeyAction::Press) close(-1);
        break;
      default: break;
    }
  }

  void draw(Gfx& g) override {
    const Theme& t = theme();
    int32_t w = screenW();
    drawTitleBar(g, title_);
    g.setFont(fontBody());
    int32_t top = kBarH + 2;
    int32_t rowH = 15;
    int rows = (screenH() - kFooterH - top) / rowH;
    if (index_ < scroll_) scroll_ = index_;
    if (index_ >= scroll_ + rows) scroll_ = index_ - rows + 1;
    for (int r = 0; r < rows && scroll_ + r < static_cast<int>(options_.size()); r++) {
      int k = scroll_ + r;
      int32_t y = top + r * rowH;
      bool sel = k == index_;
      if (sel) {
        g.fillRect(0, y, w, rowH, t.select);
        g.fillRect(0, y, 2, rowH, t.accent);
      }
      g.drawCircle(10, y + rowH / 2, 4, sel ? t.accent : t.textDim);
      if (k == current_) g.fillCircle(10, y + rowH / 2, 2, t.accent);
      g.setTextDatum(textdatum_t::middle_left);
      g.setTextColor(sel ? t.text : t.textDim);
      g.drawString(fitText(g, options_[k], w - 24), 20, y + rowH / 2);
    }
  }

  void dismiss() {
    done_ = nullptr;
    cancelled_ = nullptr;
    if (onStack(this)) {
      popTo(this);
      pop();
    }
  }

 private:
  String title_;
  std::vector<String> options_;
  int index_ = 0;
  int current_ = 0;
  int scroll_ = 0;
  std::function<void(int)> done_;
  std::function<void()> cancelled_;

  void close(int picked) {
    auto done = std::move(done_);
    auto cancelled = std::move(cancelled_);
    done_ = nullptr;
    cancelled_ = nullptr;
    pop();
    if (picked >= 0) {
      if (done) done(picked);
    } else if (cancelled) {
      cancelled();
    }
  }
};

ChoiceScreen g_choice;

}  // namespace

TrackingInfo g_tracking;

// ------------------------------------------------------------ dialogs

void openConfirm(const String& title, const String& text, std::function<void(bool)> result, const char* yes,
                 const char* no, bool defaultYes) {
  if (onStack(&g_confirm)) g_confirm.dismiss();
  g_confirm.open(title, text, std::move(result), yes, no, defaultYes);
}

void openMessage(const String& title, const String& text, std::function<void()> closed) {
  g_message.open(title, text, std::move(closed), false, 0);
}

void openBusyMessage(const String& title, const String& text, std::function<void()> closed, bool busy,
                     uint32_t timeoutMs) {
  g_message.open(title, text, std::move(closed), busy, timeoutMs);
}

void openChoiceList(const String& title, const std::vector<String>& options, int selected,
                    std::function<void(int)> done, std::function<void()> cancelled) {
  if (onStack(&g_choice)) g_choice.dismiss();
  g_choice.open(title, options, selected, std::move(done), std::move(cancelled));
}

Screen* confirmScreen() { return &g_confirm; }
Screen* messageScreen() { return &g_message; }
Screen* choiceScreen() { return &g_choice; }
void dismissConfirm() { g_confirm.dismiss(); }
void dismissMessage() { g_message.dismiss(); }
void dismissChoice() { g_choice.dismiss(); }

// ------------------------------------------------------------ roots

void showLoading() {
  setRoot(&g_loading);
}

void setLoadingText(const String& text) {
  g_loading.loadingText = text;
  invalidate();
}

void showMenu() {
  g_trackingPending = false;
  Screen* menu = menuScreen();
  if (onStack(menu)) {
    // Already under whatever the user has open (or over tracking): leave
    // their dialogs alone, but drop the tracking screen beneath.
    if (onStack(skyScreen())) {
      popTo(menu);
      setRoot(menu);
    }
    return;
  }
  setRoot(menu);
}

void showTracking() {
  if (onStack(skyScreen())) {
    return;
  }
  Screen* s = top();
  if (s == nullptr || s == menuScreen() || s == &g_loading) {
    skyScreenReset();
    setRoot(skyScreen());
  } else {
    // Don't yank the user out of the keyboard or a test.
    g_trackingPending = true;
    toast(String("Tracking ") + g_tracking.target, 2000);
  }
}

bool trackingActive() {
  return onStack(skyScreen()) || g_trackingPending;
}

void screensPoll() {
  if (g_trackingPending && top() == menuScreen()) {
    g_trackingPending = false;
    skyScreenReset();
    setRoot(skyScreen());
  }
}

}  // namespace ui
