// The sky view (tracking screen): draws the Pi's projected scene plus local
// overlays. See ui_sky.h and PROTOCOL.md ("Sky scene").
#include "ui_sky.h"

#include <cmath>
#include <vector>

#include "estop.h"
#include "motors.h"
#include "stepper.h"
#include "ui_dialogs.h"
#include "ui_screens.h"

namespace ui {

namespace {

constexpr int16_t kStripH = 12;
constexpr int16_t kPenUp = -32768;
constexpr int kTrailLength = 16;
constexpr uint32_t kLockPulseMs = 700;

// Layer and label styles, as the Pi numbers them.
enum LayerStyle : uint8_t {
  kConstellation = 0,
  kEquatorialGrid = 1,
  kEquator = 2,
  kHorizonGrid = 3,
  kHorizonLine = 4,
  kEcliptic = 5,
};
enum LabelStyle : uint8_t {
  kStarName = 0,
  kConstellationName = 1,
  kCardinal = 2,
  kEquatorialLabel = 3,
  kHorizonLabel = 4,
  kPlanetName = 5,
};

struct SkyStar {
  int16_t x, y;
  uint8_t r4;     // radius x4
  uint8_t level;  // brightness 0-255
};

struct SkyLayer {
  uint8_t style;
  std::vector<int16_t> pts;  // x,y pairs; (kPenUp, kPenUp) lifts the pen
};

struct SkyLabel {
  int16_t x, y;
  uint8_t style;
  String text;
};

struct SkyPlanet {
  int16_t x, y;
  uint8_t kind;   // 0 Sun, 1 Moon, 2 Mercury ... 9 Pluto
  uint8_t r;      // radius px
  uint8_t illum;  // lit fraction 0-100; 255 = not given
  uint8_t limb;   // direction to the Sun on screen, 0-255 = 0-360 deg, 0 = right, CCW
  String name;
};

struct SkyScene {
  bool valid = false;
  uint16_t w = 0, h = 0;
  uint8_t flags = 0;  // bit0 target present
  int16_t tx = 0, ty = 0;
  int16_t scopeAlt10 = 0;
  uint16_t scopeAz10 = 0;
  int16_t targetAlt10 = 0;
  uint16_t targetAz10 = 0;
  uint16_t distCenti = 0xFFFF;
  uint16_t fovDeci = 0;
  std::vector<SkyStar> stars;
  std::vector<SkyLayer> layers;
  uint8_t horizonStep = 0;
  std::vector<int16_t> horizon;  // first row below the horizon, per column group
  std::vector<SkyLabel> labels;
  std::vector<SkyPlanet> planets;
  uint32_t receivedMs = 0;
};

SkyScene g_scene;

struct TrailPoint {
  int16_t alt10;
  uint16_t az10;
};
TrailPoint g_trail[kTrailLength];
int g_trailCount = 0;

struct Reader {
  const uint8_t* p = nullptr;
  const uint8_t* end = nullptr;
  bool ok = true;

  bool need(size_t n) {
    if (ok && static_cast<size_t>(end - p) < n) ok = false;
    return ok;
  }
  uint8_t u8() { return need(1) ? *p++ : 0; }
  uint16_t u16() {
    if (!need(2)) return 0;
    uint16_t v = static_cast<uint16_t>(p[0] | (p[1] << 8));
    p += 2;
    return v;
  }
  int16_t i16() { return static_cast<int16_t>(u16()); }
  String str(uint8_t n) {
    String s;
    if (!need(n)) return s;
    s.reserve(n);
    for (uint8_t i = 0; i < n; i++) s += static_cast<char>(p[i]);
    p += n;
    return s;
  }
};

// ------------------------------------------------------------ colours

uint16_t nightify(uint16_t c) {
  uint8_t r = (c >> 8) & 0xF8, g = (c >> 3) & 0xFC, b = (c << 3) & 0xF8;
  uint8_t m = r > g ? (r > b ? r : b) : (g > b ? g : b);
  return rgb(m, 0, 0);
}

uint16_t tone(uint16_t c) {
  return settings().theme == 1 ? nightify(c) : c;
}

uint16_t gray(uint8_t level) {
  return tone(rgb(level, level, level));
}

uint16_t layerColor(uint8_t style) {
  switch (style) {
    case kConstellation: return tone(rgb(72, 100, 150));
    case kEquatorialGrid: return tone(rgb(92, 54, 36));
    case kEquator: return tone(rgb(140, 82, 55));
    case kHorizonGrid: return tone(rgb(38, 56, 96));
    case kHorizonLine: return tone(rgb(60, 150, 60));
    case kEcliptic: return tone(rgb(160, 135, 0));
    default: return tone(rgb(80, 80, 80));
  }
}

uint16_t labelColor(uint8_t style) {
  switch (style) {
    case kStarName: return tone(rgb(209, 185, 138));
    case kConstellationName: return tone(rgb(108, 140, 184));
    case kCardinal: return tone(rgb(110, 160, 255));
    case kEquatorialLabel: return tone(rgb(153, 90, 60));
    case kHorizonLabel: return tone(rgb(70, 100, 160));
    case kPlanetName: return tone(rgb(255, 200, 120));
    default: return tone(rgb(150, 150, 150));
  }
}

uint16_t planetColor(uint8_t kind) {
  static const uint16_t kColors[] = {
      rgb(255, 220, 80),  rgb(225, 225, 215), rgb(170, 160, 150), rgb(255, 240, 190), rgb(230, 110, 60),
      rgb(225, 195, 150), rgb(230, 200, 120), rgb(150, 220, 230), rgb(90, 130, 255),  rgb(160, 140, 130),
  };
  return tone(kind < sizeof(kColors) / sizeof(kColors[0]) ? kColors[kind] : rgb(200, 200, 200));
}

const uint16_t kScopeGreen = rgb(0, 255, 0);    // the star map's telescope marker
const uint16_t kTargetOrange = rgb(255, 165, 0);  // its search highlight

// ------------------------------------------------------------ scene drawing

void drawStar(Gfx& g, int32_t x, int32_t y, uint8_t r4, uint8_t level) {
  uint16_t c = gray(level);
  if (r4 < 4) {
    g.drawPixel(x, y, c);
  } else if (r4 < 7) {
    // A soft plus: bright centre, half-bright arms.
    uint16_t half = gray(level / 2);
    g.drawPixel(x, y, c);
    g.drawPixel(x - 1, y, half);
    g.drawPixel(x + 1, y, half);
    g.drawPixel(x, y - 1, half);
    g.drawPixel(x, y + 1, half);
  } else {
    g.fillSmoothCircle(x, y, (r4 + 2) / 4, c);
  }
}

void drawMoonPhase(Gfx& g, int32_t cx, int32_t cy, int32_t r, uint8_t illum, uint8_t limb) {
  float k = illum > 100 ? 1.0f : illum / 100.0f;
  float a = limb * 6.2831853f / 256.0f;
  float dx = cosf(a), dy = -sinf(a);  // toward the Sun, screen axes
  uint16_t lit = planetColor(1);
  uint16_t dark = tone(rgb(45, 45, 52));
  for (int32_t y = -r; y <= r; y++) {
    for (int32_t x = -r; x <= r; x++) {
      int32_t d2 = x * x + y * y;
      if (d2 > r * r) continue;
      float u = x * dx + y * dy;   // along the Sun direction
      float v = -x * dy + y * dx;  // across it
      float half = sqrtf(std::max(0.0f, static_cast<float>(r * r) - v * v));
      g.drawPixel(cx + x, cy + y, u > (1.0f - 2.0f * k) * half ? lit : dark);
    }
  }
}

void drawScene(Gfx& g, const SkyScene& s, int32_t ox, int32_t oy, int32_t w, int32_t h) {
  g.setClipRect(ox, oy, w, h);

  // Below-horizon tint, then the horizon itself.
  if (s.horizonStep > 0) {
    uint16_t tint = tone(rgb(12, 36, 12));
    int32_t step = s.horizonStep;
    for (size_t i = 0; i < s.horizon.size(); i++) {
      int32_t y = s.horizon[i];
      if (y >= h) continue;
      if (y < 0) y = 0;
      g.fillRect(ox + static_cast<int32_t>(i) * step, oy + y, step, h - y, tint);
    }
    uint16_t line = layerColor(kHorizonLine);
    for (size_t i = 1; i < s.horizon.size(); i++) {
      int32_t y0 = s.horizon[i - 1], y1 = s.horizon[i];
      if (y0 < 0 || y1 < 0 || y0 >= h || y1 >= h) continue;
      g.drawLine(ox + (static_cast<int32_t>(i) - 1) * step + step / 2, oy + y0,
                 ox + static_cast<int32_t>(i) * step + step / 2, oy + y1, line);
    }
  }

  for (const SkyLayer& layer : s.layers) {
    uint16_t c = layerColor(layer.style);
    bool smooth = layer.style == kConstellation || layer.style == kEcliptic;
    bool have = false;
    int32_t px = 0, py = 0;
    for (size_t i = 0; i + 1 < layer.pts.size(); i += 2) {
      int16_t x = layer.pts[i], y = layer.pts[i + 1];
      if (x == kPenUp) {
        have = false;
        continue;
      }
      if (have) {
        if (smooth) {
          g.drawSmoothLine(ox + px, oy + py, ox + x, oy + y, c);
        } else {
          g.drawLine(ox + px, oy + py, ox + x, oy + y, c);
        }
      }
      px = x;
      py = y;
      have = true;
    }
  }

  for (const SkyStar& st : s.stars) {
    drawStar(g, ox + st.x, oy + st.y, st.r4, st.level);
  }

  g.setFont(fontSmall());
  for (const SkyLabel& l : s.labels) {
    g.setTextColor(labelColor(l.style));
    g.setTextDatum(l.style == kConstellationName || l.style == kCardinal ? textdatum_t::middle_center
                                                                          : textdatum_t::middle_left);
    g.drawString(l.text, ox + l.x, oy + l.y);
  }

  for (const SkyPlanet& p : s.planets) {
    int32_t x = ox + p.x, y = oy + p.y;
    if (p.kind == 1 && p.illum <= 100) {
      drawMoonPhase(g, x, y, std::max<int32_t>(3, p.r), p.illum, p.limb);
    } else {
      g.fillSmoothCircle(x, y, std::max<int32_t>(1, p.r), planetColor(p.kind));
    }
    // Names come as labels (the Pi keeps them clear of each other).
  }
  g.clearClipRect();
}

// ------------------------------------------------------------ formatting

String formatRa(double deg) {
  if (!std::isfinite(deg)) return "--";
  double hours = fmod(fmod(deg, 360.0) + 360.0, 360.0) / 15.0;
  long total = lround(hours * 3600.0) % (24 * 3600);
  char buf[16];
  snprintf(buf, sizeof(buf), "%02ldh %02ldm %02lds", total / 3600, (total / 60) % 60, total % 60);
  return buf;
}

String formatDec(double deg) {
  if (!std::isfinite(deg)) return "--";
  char sign = deg < 0 ? '-' : '+';
  long total = lround(fabs(deg) * 3600.0);
  char buf[20];
  snprintf(buf, sizeof(buf), "%c%02ld* %02ld' %02ld\"", sign, total / 3600, (total / 60) % 60, total % 60);
  return buf;
}

String formatDuration(uint32_t seconds) {
  char buf[20];
  if (seconds >= 3600) {
    snprintf(buf, sizeof(buf), "%u:%02u:%02u", static_cast<unsigned>(seconds / 3600),
             static_cast<unsigned>((seconds / 60) % 60), static_cast<unsigned>(seconds % 60));
  } else {
    snprintf(buf, sizeof(buf), "%u:%02u", static_cast<unsigned>(seconds / 60), static_cast<unsigned>(seconds % 60));
  }
  return buf;
}

String axisLine(int axis) {
  const char* name = axis == 0 ? "RA " : "Dec";
  if (!stepperIsMoving(axis)) {
    return String(name) + (motorEngaged(axis) ? "  holding" : "  off");
  }
  double pulses = stepperCurrentRate(axis);
  uint16_t micro = motorMicrosteps(axis);
  double sps = micro > 0 ? pulses * motorLogicalMicrosteps(axis) / micro : 0.0;
  char buf[32];
  snprintf(buf, sizeof(buf), "%s %7.2f/s 1/%u", name, sps, static_cast<unsigned>(micro));
  return buf;
}

// ------------------------------------------------------------ the screen

class SkyScreen : public Screen {
 public:
  const char* name() const override { return "tracking"; }
  uint32_t refreshMs() const override { return 120; }
  bool returnNavigates() const override { return false; }
  String footerHint() const override { return page_ == 0 ? "\x11\x10zoom \x1b\x1bstop" : "\x1b\x1b stop"; }

  void reset() {
    page_ = 0;
    lastState_ = "";
  }

  void stateJson(JsonObject out) const override {
    out["page"] = page_ == 0 ? "sky" : "details";
    out["scene"] = g_scene.valid;
    out["scene_age_ms"] = g_scene.valid ? millis() - g_scene.receivedMs : 0;
    out["stars"] = g_scene.stars.size();
    out["labels"] = g_scene.labels.size();
    out["radar"] = settings().skyRadar;
  }

  void key(const KeyEvent& e) override {
    if (e.key == Key::Return && e.action == KeyAction::Press) {
      if (e.doublePress) {
        if (!panelStopPending() || stepperIsMoving(0) || stepperIsMoving(1)) {
          panelStopTrigger("return_double");
        }
        g_tracking.state = "stopped";
        showMenu();
        toast("Tracking stopped");
      } else {
        toast("Press RETURN twice to stop", 1200);
      }
      return;
    }
    if (e.key == Key::Select && e.action == KeyAction::Long) {
      // Browse the menu while tracking carries on; RETURN comes back here.
      push(menuScreen());
      return;
    }
    if (e.action != KeyAction::Press) return;
    switch (e.key) {
      case Key::Select: page_ = 1 - page_; break;
      case Key::Left:
      case Key::Right: {
        JsonDocument doc;
        doc["zoom"] = e.key == Key::Right ? 1 : -1;
        emitUiEvent(doc, "sky");
        break;
      }
      case Key::Up:
      case Key::Down:
        settings().skyRadar = !settings().skyRadar;
        saveSettings();
        break;
      default: break;
    }
  }

  void tick(uint32_t now) override {
    // Per-axis progress for the arcs: the size of each finite move is the
    // largest remaining count seen since it started.
    for (int a = 0; a < kAxisCount; a++) {
      bool moving = stepperIsMoving(a);
      int32_t rem = stepperPulsesRemaining(a);
      if (moving && rem > 0 && (!wasMoving_[a] || rem > total_[a])) total_[a] = rem;
      if (!moving) total_[a] = 0;
      wasMoving_[a] = moving;
    }
    if (g_tracking.state != lastState_) {
      if (g_tracking.state == "tracking") lockAt_ = now;
      lastState_ = g_tracking.state;
    }
  }

  void draw(Gfx& g) override {
    if (page_ == 1) {
      drawDetails(g);
      return;
    }
    int32_t w = screenW();
    int32_t h = skyViewHeight();
    int32_t top = kStripH;
    const SkyScene& s = g_scene;
    g.fillRect(0, top, w, h, tone(rgb(0, 0, 0)));
    int32_t ox = (w - static_cast<int32_t>(s.w)) / 2;
    if (s.valid) {
      drawScene(g, s, ox, top, w, h);
    } else {
      g.setFont(fontSmall());
      g.setTextDatum(textdatum_t::middle_center);
      g.setTextColor(theme().textDim);
      g.drawString("waiting for the sky", w / 2, top + h / 2 + 24);
    }
    int32_t cx = w / 2;
    int32_t cy = top + h / 2;
    drawTarget(g, s, ox, top, w, h, cx, cy);
    drawArcs(g, cx, cy);
    drawReticle(g, cx, cy);
    if (settings().skyRadar && s.valid) drawRadar(g, s, top, h);
    if (s.valid && s.fovDeci > 0) {
      g.setFont(fontSmall());
      g.setTextDatum(textdatum_t::bottom_right);
      g.setTextColor(theme().textDim);
      char buf[12];
      snprintf(buf, sizeof(buf), "%u", static_cast<unsigned>((s.fovDeci + 5) / 10));
      int32_t tw = g.textWidth(buf);
      g.drawString(buf, w - 6, top + h - 2);
      g.drawCircle(w - 3, top + h - 9, 1, theme().textDim);
      (void)tw;
    }
    drawStrip(g, s);
  }

 private:
  int page_ = 0;
  String lastState_;
  uint32_t lockAt_ = 0;
  int32_t total_[kAxisCount] = {0, 0};
  bool wasMoving_[kAxisCount] = {false, false};

  bool locked() const { return g_tracking.state == "tracking"; }

  void drawReticle(Gfx& g, int32_t cx, int32_t cy) {
    uint16_t c = tone(kScopeGreen);
    g.drawCircle(cx, cy, 7, c);
    g.drawFastHLine(cx - 11, cy, 4, c);
    g.drawFastHLine(cx + 8, cy, 4, c);
    g.drawFastVLine(cx, cy - 11, 4, c);
    g.drawFastVLine(cx, cy + 8, 4, c);
    g.drawPixel(cx, cy, c);
  }

  // Two progress arcs round the reticle: RA on the left, Dec on the right,
  // filling bottom-up as each axis's move completes.
  void drawArcs(Gfx& g, int32_t cx, int32_t cy) {
    const Theme& t = theme();
    for (int a = 0; a < kAxisCount; a++) {
      float start = a == 0 ? 110.0f : -70.0f;
      float span = 140.0f;
      g.fillArc(cx, cy, 15, 14, start, start + span, t.surface);
      if (!stepperIsMoving(a)) continue;
      int32_t rem = stepperPulsesRemaining(a);
      uint16_t c = tone(t.warn);
      if (stepperPathActive()) {
        // A curved slew: both arcs show how far along the path it is.
        float done = static_cast<float>(stepperPathProgress());
        if (done > 0.0f) {
          if (a == 0) g.fillArc(cx, cy, 15, 14, start + span * (1.0f - done), start + span, c);
          else g.fillArc(cx, cy, 15, 14, start, start + span * done, c);
        }
        continue;
      }
      if (rem < 0) {
        // Continuous (tracking): a short segment sweeping along the arc.
        float pos = static_cast<float>((millis() / 25) % 140);
        float a0 = a == 0 ? start + pos : start + span - pos;
        g.fillArc(cx, cy, 15, 14, std::max(start, a0 - 12.0f), std::min(start + span, a0 + 12.0f),
                  tone(t.ok));
        continue;
      }
      float done = total_[a] > 0 ? 1.0f - static_cast<float>(rem) / total_[a] : 0.0f;
      if (done <= 0.0f) continue;
      if (a == 0) {
        g.fillArc(cx, cy, 15, 14, start + span - span * done, start + span, c);
      } else {
        g.fillArc(cx, cy, 15, 14, start, start + span * done, c);
      }
    }
  }

  void drawTarget(Gfx& g, const SkyScene& s, int32_t ox, int32_t top, int32_t w, int32_t h, int32_t cx, int32_t cy) {
    if (!s.valid || !(s.flags & 1)) return;
    int32_t tx = ox + s.tx, ty = top + s.ty;
    bool inside = tx >= 0 && tx < w && ty >= top && ty < top + h;
    uint16_t ring = tone(locked() ? kScopeGreen : kTargetOrange);
    if (!inside) {
      // Off screen: an arrow on the edge pointing at it.
      float ang = atan2f(static_cast<float>(ty - cy), static_cast<float>(tx - cx));
      float ex = cosf(ang), ey = sinf(ang);
      float sx = (w / 2 - 10) / std::max(0.001f, fabsf(ex));
      float sy = (h / 2 - 10) / std::max(0.001f, fabsf(ey));
      float d = std::min(sx, sy);
      int32_t ax = cx + static_cast<int32_t>(ex * d), ay = cy + static_cast<int32_t>(ey * d);
      int32_t bx = ax - static_cast<int32_t>(ex * 7), by = ay - static_cast<int32_t>(ey * 7);
      g.fillTriangle(ax, ay, bx - static_cast<int32_t>(ey * 4), by + static_cast<int32_t>(ex * 4),
                     bx + static_cast<int32_t>(ey * 4), by - static_cast<int32_t>(ex * 4), ring);
      return;
    }
    if (locked()) {
      g.drawCircle(tx, ty, 5, ring);
      uint32_t since = millis() - lockAt_;
      if (lockAt_ != 0 && since < kLockPulseMs) {
        int32_t r = 5 + static_cast<int32_t>(since * 22 / kLockPulseMs);
        g.drawCircle(tx, ty, r, ring);
        g.drawCircle(tx, ty, r + 1, ring);
      }
      return;
    }
    // Lock-on rings: they close in as the remaining error drops.
    float dist = s.distCenti == 0xFFFF ? 5.0f : s.distCenti / 100.0f;
    int32_t r = 5 + static_cast<int32_t>(std::min(21.0f, dist * 6.0f));
    g.drawCircle(tx, ty, 3, ring);
    g.drawCircle(tx, ty, r, ring);
    if (r > 9) g.drawCircle(tx, ty, r * 2 / 3, ring);
  }

  // Small all-sky chart: horizon circle, N at the top and E on the left as
  // on a star chart, the scope (green, with a fading trail) and the target.
  void drawRadar(Gfx& g, const SkyScene& s, int32_t top, int32_t h) {
    const Theme& t = theme();
    const int32_t R = 17;
    int32_t cx = R + 3, cy = top + h - R - 3;
    g.fillCircle(cx, cy, R, t.bar);
    g.drawCircle(cx, cy, R, t.textDim);
    g.drawCircle(cx, cy, R * 2 / 3, t.surface);
    g.drawCircle(cx, cy, R / 3, t.surface);
    g.drawFastVLine(cx, cy - R, 3, t.text);
    g.drawFastVLine(cx, cy + R - 2, 3, t.textDim);
    g.drawFastHLine(cx - R, cy, 3, t.textDim);
    g.drawFastHLine(cx + R - 2, cy, 3, t.textDim);
    g.setFont(fontSmall());
    g.setTextDatum(textdatum_t::top_center);
    g.setTextColor(t.text);
    g.drawString("N", cx, cy - R + 3);
    auto place = [&](int16_t alt10, uint16_t az10, int32_t& x, int32_t& y) {
      float alt = std::max(0.0f, alt10 / 10.0f);
      float r = R * (90.0f - alt) / 90.0f;
      float az = az10 / 10.0f * 0.0174533f;
      x = cx - static_cast<int32_t>(r * sinf(az));
      y = cy - static_cast<int32_t>(r * cosf(az));
    };
    for (int i = 0; i < g_trailCount; i++) {
      int32_t x, y;
      place(g_trail[i].alt10, g_trail[i].az10, x, y);
      uint8_t level = static_cast<uint8_t>(40 + 120 * (i + 1) / kTrailLength);
      g.drawPixel(x, y, tone(rgb(0, level, 0)));
    }
    int32_t x, y;
    if (s.flags & 1) {
      place(s.targetAlt10, s.targetAz10, x, y);
      g.drawCircle(x, y, 2, tone(kTargetOrange));
    }
    place(s.scopeAlt10, s.scopeAz10, x, y);
    g.fillCircle(x, y, 2, tone(kScopeGreen));
  }

  void drawStrip(Gfx& g, const SkyScene& s) {
    const Theme& t = theme();
    int32_t w = screenW();
    g.fillRect(0, 0, w, kStripH, t.bar);
    const String& state = g_tracking.state;
    uint16_t dot = state == "tracking" ? t.ok : (state == "stopped" ? t.bad : (state == "idle" ? t.textDim : t.warn));
    g.fillCircle(4, kStripH / 2, 2, dot);

    String right;
    if (locked()) {
      right = "LOCK " + formatDuration((millis() - g_tracking.trackingSinceMs) / 1000);
    } else if (s.valid && (s.flags & 1) && s.distCenti != 0xFFFF) {
      float dist = s.distCenti / 100.0f;
      right = dist >= 10.0f ? String(dist, 0) : String(dist, dist >= 1.0f ? 1 : 2);
      right += "*";
      uint32_t eta = etaSeconds();
      if (eta > 0) right += " " + formatDuration(eta);
    }
    g.setFont(fontSmall());
    int32_t rightW = 0;
    if (right.length() > 0) {
      // The '*' becomes a drawn degree sign.
      int star = right.indexOf('*');
      String before = star >= 0 ? right.substring(0, star) : right;
      String after = star >= 0 ? right.substring(star + 1) : String("");
      int32_t afterW = g.textWidth(after);
      int32_t beforeW = g.textWidth(before);
      rightW = beforeW + (star >= 0 ? 4 : 0) + afterW;
      int32_t x = w - 3 - rightW;
      g.setTextDatum(textdatum_t::middle_left);
      g.setTextColor(locked() ? t.ok : t.text);
      g.drawString(before, x, kStripH / 2);
      x += beforeW;
      if (star >= 0) {
        g.drawCircle(x + 1, kStripH / 2 - 3, 1, locked() ? t.ok : t.text);
        x += 4;
      }
      g.drawString(after, x, kStripH / 2);
    }
    g.setFont(fontBody());
    g.setTextDatum(textdatum_t::middle_left);
    g.setTextColor(t.accent);
    String name = g_tracking.target.length() > 0 ? g_tracking.target : String("Sky");
    g.drawString(fitText(g, name, w - 12 - rightW - 4), 9, kStripH / 2);
  }

  // Seconds until both axes finish their finite moves (0 = none running).
  uint32_t etaSeconds() const {
    if (stepperPathActive()) return static_cast<uint32_t>(stepperPathEtaSeconds() + 0.5);
    float worst = 0.0f;
    for (int a = 0; a < kAxisCount; a++) {
      int32_t rem = stepperPulsesRemaining(a);
      double rate = stepperCurrentRate(a);
      if (rem > 0 && rate > 1.0) worst = std::max(worst, static_cast<float>(rem / rate));
    }
    return static_cast<uint32_t>(worst + 0.5f);
  }

  void drawDetails(Gfx& g) {
    const Theme& t = theme();
    TrackingInfo& ti = g_tracking;
    int32_t w = screenW();
    drawTitleBar(g, ti.target.length() > 0 ? ti.target : String("Telescope"), ti.state);
    int32_t y = kBarH + 4;
    g.setFont(fontBody());
    auto row = [&](const char* label, const String& value) {
      g.setTextDatum(textdatum_t::top_left);
      g.setTextColor(t.textDim);
      g.drawString(label, 4, y);
      drawDegreesText(g, value, 34, y, t.text);
      y += 12;
    };
    g.setFont(fontSmall());
    g.setTextColor(t.accent);
    g.setTextDatum(textdatum_t::top_left);
    g.drawString("target", 4, y);
    y += 10;
    g.setFont(fontBody());
    row("RA", formatRa(ti.ra));
    row("Dec", formatDec(ti.dec));
    if (std::isfinite(ti.mag)) row("Mag", String(ti.mag, 1));
    g.setFont(fontSmall());
    g.setTextColor(t.accent);
    g.setTextDatum(textdatum_t::top_left);
    g.drawString("scope", 4, y + 2);
    y += 12;
    g.setFont(fontBody());
    row("RA", formatRa(ti.raNow));
    row("Dec", formatDec(ti.decNow));
    if (g_scene.valid) {
      row("Alt", String(g_scene.scopeAlt10 / 10.0f, 1) + "*");
      row("Az", String(g_scene.scopeAz10 / 10.0f, 1) + "*");
    }
    g.setFont(fontSmall());
    g.setTextColor(t.text);
    g.setTextDatum(textdatum_t::top_left);
    if (y + 22 < screenH() - kFooterH) {
      g.drawString(axisLine(0), 4, y + 2);
      g.drawString(axisLine(1), 4, y + 12);
    }
    (void)w;
  }
};

SkyScreen g_skyScreen;

}  // namespace

int16_t skyViewWidth() {
  return screenW();
}

int16_t skyViewHeight() {
  return screenH() - kStripH - kFooterH;
}

bool skyAcceptScene(const uint8_t* data, size_t len, String* error) {
  Reader r;
  r.p = data;
  r.end = data + len;
  if (len < 4 || memcmp(data, "SKY1", 4) != 0) {
    if (error) *error = "not a SKY1 scene";
    return false;
  }
  r.p += 4;
  SkyScene s;
  s.w = r.u16();
  s.h = r.u16();
  s.flags = r.u8();
  r.u8();  // reserved
  s.tx = r.i16();
  s.ty = r.i16();
  s.scopeAlt10 = r.i16();
  s.scopeAz10 = r.u16();
  s.targetAlt10 = r.i16();
  s.targetAz10 = r.u16();
  s.distCenti = r.u16();
  s.fovDeci = r.u16();

  uint16_t nStars = r.u16();
  if (r.need(static_cast<size_t>(nStars) * 6)) {
    s.stars.resize(nStars);
    for (SkyStar& st : s.stars) {
      st.x = r.i16();
      st.y = r.i16();
      st.r4 = r.u8();
      st.level = r.u8();
    }
  }
  uint8_t nLayers = r.u8();
  for (uint8_t i = 0; i < nLayers && r.ok; i++) {
    SkyLayer layer;
    layer.style = r.u8();
    uint16_t n = r.u16();
    if (!r.need(static_cast<size_t>(n) * 4)) break;
    layer.pts.resize(static_cast<size_t>(n) * 2);
    for (uint16_t k = 0; k < n * 2; k++) layer.pts[k] = r.i16();
    s.layers.push_back(std::move(layer));
  }
  s.horizonStep = r.u8();
  uint16_t nCols = r.u16();
  if (r.need(static_cast<size_t>(nCols) * 2)) {
    s.horizon.resize(nCols);
    for (int16_t& y : s.horizon) y = r.i16();
  }
  uint8_t nLabels = r.u8();
  for (uint8_t i = 0; i < nLabels && r.ok; i++) {
    SkyLabel l;
    l.x = r.i16();
    l.y = r.i16();
    l.style = r.u8();
    l.text = r.str(r.u8());
    s.labels.push_back(std::move(l));
  }
  uint8_t nPlanets = r.u8();
  for (uint8_t i = 0; i < nPlanets && r.ok; i++) {
    SkyPlanet p;
    p.x = r.i16();
    p.y = r.i16();
    p.kind = r.u8();
    p.r = r.u8();
    p.illum = r.u8();
    p.limb = r.u8();
    p.name = r.str(r.u8());
    s.planets.push_back(std::move(p));
  }
  if (!r.ok) {
    if (error) *error = "scene truncated";
    return false;
  }
  s.valid = true;
  s.receivedMs = millis();

  // Trail of where the scope has been, for the radar.
  bool moved = g_trailCount == 0;
  if (!moved) {
    const TrailPoint& last = g_trail[g_trailCount - 1];
    int daz = abs(static_cast<int>(last.az10) - static_cast<int>(s.scopeAz10));
    if (daz > 1800) daz = 3600 - daz;
    moved = abs(last.alt10 - s.scopeAlt10) + daz > 5;
  }
  if (moved) {
    if (g_trailCount == kTrailLength) {
      memmove(g_trail, g_trail + 1, sizeof(g_trail[0]) * (kTrailLength - 1));
      g_trailCount--;
    }
    g_trail[g_trailCount++] = {s.scopeAlt10, s.scopeAz10};
  }

  g_scene = std::move(s);
  invalidate();
  return true;
}

Screen* skyScreen() {
  return &g_skyScreen;
}

void skyScreenReset() {
  g_skyScreen.reset();
}

}  // namespace ui

void uiSkySceneComplete(BinaryTransfer& t) {
  String error;
  if (ui::skyAcceptScene(t.buffer, t.total, &error)) {
    hostSendOkFor(t.id);
  } else {
    hostSendErrorFor(t.id, error.c_str());
  }
}
