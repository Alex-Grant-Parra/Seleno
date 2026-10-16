// Drawing helpers shared by the UI screens.
#include <cmath>

#include "estop.h"
#include "ui_internal.h"

namespace ui {

const lgfx::IFont* fontBody() { return &fonts::DejaVu9; }
const lgfx::IFont* fontLarge() { return &fonts::DejaVu12; }
const lgfx::IFont* fontHuge() { return &fonts::DejaVu18; }
const lgfx::IFont* fontSmall() { return &fonts::Font0; }
const lgfx::IFont* fontLogo() { return &fonts::Orbitron_Light_24; }

String fitText(Gfx& g, const String& s, int32_t maxWidth) {
  if (maxWidth <= 0) return "";
  if (g.textWidth(s) <= maxWidth) return s;
  String out = s;
  while (out.length() > 0 && g.textWidth(out + "..") > maxWidth) {
    out.remove(out.length() - 1);
  }
  return out + "..";
}

void drawArrow(Gfx& g, int32_t cx, int32_t cy, int32_t size, char dir, uint16_t color) {
  switch (dir) {
    case 'l': g.fillTriangle(cx - size, cy, cx + size - 1, cy - size, cx + size - 1, cy + size, color); break;
    case 'r': g.fillTriangle(cx + size, cy, cx - size + 1, cy - size, cx - size + 1, cy + size, color); break;
    case 'u': g.fillTriangle(cx, cy - size, cx - size, cy + size - 1, cx + size, cy + size - 1, color); break;
    case 'd': g.fillTriangle(cx, cy + size, cx - size, cy - size + 1, cx + size, cy - size + 1, color); break;
    default: break;
  }
}

void drawToggle(Gfx& g, int32_t x, int32_t cy, bool on, bool focused) {
  const Theme& t = theme();
  uint16_t track = on ? t.accent : (focused ? t.textDim : t.surface);
  g.fillRoundRect(x, cy - 5, 18, 10, 5, track);
  uint16_t knob = on ? t.onAccent : t.text;
  g.fillCircle(on ? x + 13 : x + 4, cy, 3, knob);
}

void drawTitleBar(Gfx& g, const String& title, const String& right) {
  const Theme& t = theme();
  int32_t w = screenW();
  g.fillRect(0, 0, w, kBarH, t.bar);
  g.fillRect(0, kBarH - 1, w, 1, t.accent);
  int32_t rightW = 0;
  if (right.length() > 0) {
    g.setFont(fontSmall());
    g.setTextDatum(textdatum_t::middle_right);
    g.setTextColor(t.textDim);
    g.drawString(right, w - 3, kBarH / 2);
    rightW = g.textWidth(right) + 6;
  }
  g.setFont(fontBody());
  g.setTextDatum(textdatum_t::middle_left);
  g.setTextColor(t.text);
  g.drawString(fitText(g, title, w - 8 - rightW), 4, kBarH / 2);
}

// Key icons inside hint text: \x07 SELECT, \x1b RETURN, \x10 \x11 \x1e \x1f
// right/left/up/down. Everything else is Font0 text.
constexpr int32_t kIconCellW = 7;

int32_t iconTextWidth(Gfx& g, const String& s) {
  g.setFont(fontSmall());
  int32_t w = 0;
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    w += (c < 0x20) ? kIconCellW : 6;
  }
  return w;
}

void drawIconText(Gfx& g, const String& s, int32_t x, int32_t cy, uint16_t color) {
  g.setFont(fontSmall());
  g.setTextDatum(textdatum_t::middle_left);
  g.setTextColor(color);
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    int32_t mid = x + kIconCellW / 2;
    switch (c) {
      case '\x07': g.fillCircle(mid, cy, 2, color); x += kIconCellW; continue;
      case '\x10': drawArrow(g, mid, cy, 2, 'r', color); x += kIconCellW; continue;
      case '\x11': drawArrow(g, mid, cy, 2, 'l', color); x += kIconCellW; continue;
      case '\x1e': drawArrow(g, mid, cy, 2, 'u', color); x += kIconCellW; continue;
      case '\x1f': drawArrow(g, mid, cy, 2, 'd', color); x += kIconCellW; continue;
      case '\x1b':
        // A RETURN key: a hooked arrow pointing left.
        g.drawFastVLine(mid + 2, cy - 3, 4, color);
        g.drawFastHLine(mid - 2, cy + 1, 5, color);
        drawArrow(g, mid - 2, cy + 1, 2, 'l', color);
        x += kIconCellW;
        continue;
      default: break;
    }
    if (c < 0x20) {
      x += kIconCellW;
      continue;
    }
    char one[2] = {c, 0};
    g.drawString(one, x, cy);
    x += 6;
  }
}

void drawFooter(Gfx& g, const String& hint) {
  const Theme& t = theme();
  int32_t w = screenW();
  int32_t y = screenH() - kFooterH;
  int32_t cy = y + kFooterH / 2;
  g.fillRect(0, y, w, kFooterH, t.bar);

  // Pi link dot.
  Link l = link();
  uint16_t linkColor = l == Link::Connected ? t.ok : (l == Link::Waiting ? t.warn : t.bad);
  g.setFont(fontSmall());
  g.setTextDatum(textdatum_t::middle_left);
  g.setTextColor(t.textDim);
  g.drawString("Pi", 3, cy);
  g.fillCircle(19, cy, 2, linkColor);
  int32_t x = 25;

  if (estopLatched()) {
    g.fillRoundRect(x, y + 2, 26, kFooterH - 3, 2, t.bad);
    g.setTextColor(t.bg);
    g.drawString("STOP", x + 1, cy);
    x += 29;
  } else if (panelStopPending()) {
    g.fillRoundRect(x, y + 2, 26, kFooterH - 3, 2, t.warn);
    g.setTextColor(t.bg);
    g.drawString("HOLD", x + 1, cy);
    x += 29;
  }

  int32_t hintW = hint.length() > 0 ? iconTextWidth(g, hint) : 0;
  int32_t hintX = w - 3 - hintW;
  String clock = clockText();
  if (clock.length() > 0 && x + 30 + 4 < hintX) {
    g.setTextColor(t.textDim);
    g.drawString(clock, x, cy);
  }
  if (hintW > 0) {
    drawIconText(g, hint, hintX, cy, t.textDim);
  }
}

void drawDegreesText(Gfx& g, const String& s, int32_t x, int32_t y, uint16_t color) {
  // y is the text top (top_left datum).
  g.setTextDatum(textdatum_t::top_left);
  g.setTextColor(color);
  int start = 0;
  int n = s.length();
  for (int i = 0; i <= n; i++) {
    if (i < n && s[i] != '*') continue;
    String part = s.substring(start, i);
    if (part.length() > 0) {
      g.drawString(part, x, y);
      x += g.textWidth(part);
    }
    if (i < n) {
      g.drawCircle(x + 2, y + 2, 1, color);
      x += 5;
    }
    start = i + 1;
  }
}

int32_t drawWrapped(Gfx& g, const String& s, int32_t x, int32_t y, int32_t w, uint16_t color, int32_t lineH) {
  g.setTextDatum(textdatum_t::top_left);
  g.setTextColor(color);
  int32_t used = 0;
  int pos = 0;
  int n = s.length();
  while (pos < n) {
    // Honour explicit newlines, then fill the line word by word.
    int lineEnd = s.indexOf('\n', pos);
    if (lineEnd < 0) lineEnd = n;
    int cut = lineEnd;
    while (cut > pos && g.textWidth(s.substring(pos, cut)) > w) {
      int space = s.lastIndexOf(' ', cut - 1);
      if (space > pos) {
        cut = space;
      } else {
        // One long word (a URL, a key): break it anywhere.
        cut--;
      }
    }
    if (cut == pos) cut = pos + 1;
    g.drawString(s.substring(pos, cut), x, y + used);
    used += lineH;
    pos = cut;
    while (pos < n && (s[pos] == ' ')) pos++;
    if (pos < n && s[pos] == '\n') pos++;
  }
  return used;
}

void drawMoon(Gfx& g, int32_t cx, int32_t cy, int32_t r, float phase) {
  const Theme& t = theme();
  // A lit disc with a shadow disc sliding across it: new -> full -> new.
  g.fillCircle(cx, cy, r, t.accent);
  float offset = (phase * 2.0f - 1.0f) * 2.2f * r;
  g.fillCircle(cx + static_cast<int32_t>(offset), cy - 2, r, t.bg);
  g.drawCircle(cx, cy, r, t.surface);
}

}  // namespace ui
