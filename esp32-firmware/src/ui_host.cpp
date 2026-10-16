// {"cmd":"ui", ...}: the Pi's side of the front-panel UI (see PROTOCOL.md).
#include <cmath>

#include "config.h"
#include "display.h"
#include "estop.h"
#include "host_link.h"
#include "ui.h"
#include "ui_dialogs.h"
#include "ui_internal.h"
#include "ui_menu.h"
#include "ui_screens.h"
#include "ui_sky.h"

namespace ui {

namespace {

// A Pi prompt: shown at once, or as soon as the dialog it needs is free.
struct Prompt {
  String id;
  String kind;  // text, confirm, message, busy, choice, code
  String title;
  String text;
  String value;
  String subtitle;
  uint16_t maxLength = 64;
  std::vector<String> options;
  int selected = 0;
  String yes = "Yes";
  String no = "No";
  bool defaultYes = false;
  uint32_t timeoutMs = 0;
  Alert alert = Alert::None;  // confirm: "style" warn or danger
};

Prompt g_pending;
bool g_hasPending = false;
String g_shownId;    // the prompt on screen now ("" = none)
String g_shownKind;

uint32_t g_releaseAtMs = 0;  // injected long press: when to let go
Key g_releaseKey = Key::Select;

Screen* dialogFor(const String& kind) {
  if (kind == "text") return keyboardScreen();
  if (kind == "confirm") return confirmScreen();
  if (kind == "choice") return choiceScreen();
  if (kind == "code") return codeScreen();
  return messageScreen();
}

void emitPromptResult(const String& id, const String& kind, std::function<void(JsonDocument&)> fill) {
  JsonDocument doc;
  doc["prompt"] = id;
  doc["kind"] = kind;
  fill(doc);
  if (g_shownId == id) {
    g_shownId = "";
    g_shownKind = "";
  }
  emitUiEvent(doc, "prompt");
}

void dismissShown() {
  if (g_shownId.length() == 0) return;
  Screen* s = dialogFor(g_shownKind);
  if (s == keyboardScreen()) dismissKeyboard();
  else if (s == confirmScreen()) dismissConfirm();
  else if (s == choiceScreen()) dismissChoice();
  else if (s == codeScreen()) dismissCode();
  else dismissMessage();
  g_shownId = "";
  g_shownKind = "";
}

void showPrompt(const Prompt& p) {
  if (hostMode()) leaveHostMode();
  wakeScreen();
  // A newer prompt replaces the Pi's previous one.
  dismissShown();
  g_shownId = p.id;
  g_shownKind = p.kind;
  String id = p.id;
  String kind = p.kind;
  if (kind == "text") {
    openKeyboard(
        p.title, p.value, p.maxLength,
        [id, kind](const String& v) { emitPromptResult(id, kind, [&v](JsonDocument& d) { d["value"] = v; }); },
        [id, kind]() { emitPromptResult(id, kind, [](JsonDocument& d) { d["cancelled"] = true; }); },
        p.subtitle.length() > 0 ? p.subtitle : p.text);
  } else if (kind == "confirm") {
    openConfirm(
        p.title, p.text,
        [id, kind](bool yes) { emitPromptResult(id, kind, [yes](JsonDocument& d) { d["value"] = yes; }); },
        p.yes.c_str(), p.no.c_str(), p.defaultYes, p.alert);
  } else if (kind == "code") {
    openCode(p.title, p.text, p.timeoutMs, [id, kind](bool ok) {
      emitPromptResult(id, kind, [ok](JsonDocument& d) {
        if (ok) {
          d["value"] = true;
        } else {
          d["cancelled"] = true;
        }
      });
    });
  } else if (kind == "choice") {
    std::vector<String> options = p.options;
    openChoiceList(
        p.title, options, p.selected,
        [id, kind, options](int i) {
          emitPromptResult(id, kind, [i, &options](JsonDocument& d) {
            d["value"] = i;
            d["option"] = options[i];
          });
        },
        [id, kind]() { emitPromptResult(id, kind, [](JsonDocument& d) { d["cancelled"] = true; }); });
  } else {
    openBusyMessage(
        p.title, p.text,
        [id, kind]() { emitPromptResult(id, kind, [](JsonDocument& d) { d["dismissed"] = true; }); },
        kind == "busy", p.timeoutMs);
  }
}

bool dialogBusyLocally(const String& kind) {
  // In use for something of the user's own (not a Pi prompt we can replace).
  Screen* s = dialogFor(kind);
  if (!onStack(s)) return false;
  return !(g_shownId.length() > 0 && dialogFor(g_shownKind) == s);
}

void base64Print(Print& out, const uint8_t* data, size_t len) {
  static const char kAlphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  char chunk[65];
  size_t used = 0;
  for (size_t i = 0; i < len; i += 3) {
    uint32_t n = static_cast<uint32_t>(data[i]) << 16;
    size_t left = len - i;
    if (left > 1) n |= static_cast<uint32_t>(data[i + 1]) << 8;
    if (left > 2) n |= data[i + 2];
    chunk[used++] = kAlphabet[(n >> 18) & 63];
    chunk[used++] = kAlphabet[(n >> 12) & 63];
    chunk[used++] = left > 1 ? kAlphabet[(n >> 6) & 63] : '=';
    chunk[used++] = left > 2 ? kAlphabet[n & 63] : '=';
    if (used >= 64) {
      chunk[used] = '\0';
      out.print(chunk);
      used = 0;
    }
  }
  if (used > 0) {
    chunk[used] = '\0';
    out.print(chunk);
  }
}

void handleScreenshot() {
  LGFX_Sprite* frame = renderForScreenshot();
  if (frame == nullptr || frame->getBuffer() == nullptr) {
    hostSendError("No frame buffer");
    return;
  }
  Print& out = hostOut();
  hostBeginRawResponse();
  out.print("{\"width\":");
  out.print(frame->width());
  out.print(",\"height\":");
  out.print(frame->height());
  out.print(",\"format\":\"rgb565be\",\"host_mode\":");
  out.print(hostMode() ? "true" : "false");
  out.print(",\"base64\":\"");
  base64Print(out, static_cast<const uint8_t*>(frame->getBuffer()),
              static_cast<size_t>(frame->width()) * frame->height() * 2);
  out.print("\"}");
  hostEndRawResponse();
}

void addState(JsonObject data) {
  statusJson(data);
  JsonArray stack = data["stack"].to<JsonArray>();
  stackJson(stack);
  Screen* s = top();
  data["screen"] = s != nullptr ? s->name() : "";
  if (s != nullptr) {
    s->stateJson(data["top"].to<JsonObject>());
  }
  menuStateJson(data["menu"].to<JsonObject>());
  data["prompt"] = g_shownId;
  data["prompt_pending"] = g_hasPending ? g_pending.id : String("");
  JsonObject tr = data["tracking"].to<JsonObject>();
  tr["state"] = g_tracking.state;
  tr["target"] = g_tracking.target;
  tr["screen"] = trackingActive();
  data["estop"] = estopLatched();
}

void settingsJson(JsonObject out) {
  Settings& s = settings();
  out["brightness"] = s.brightness;
  out["rotation"] = s.rotation;
  out["theme"] = s.theme == 1 ? "night" : "dark";
  out["dim_after_s"] = s.dimAfterS;
  out["leds_muted"] = s.ledsMuted;
}

void handleSettings(JsonDocument& req) {
  Settings& s = settings();
  bool changed = false;
  if (!req["brightness"].isNull()) {
    int v = req["brightness"].as<int>();
    s.brightness = static_cast<uint8_t>(v < 5 ? 5 : (v > 100 ? 100 : v));
    changed = true;
  }
  if (!req["rotation"].isNull()) {
    s.rotation = req["rotation"].as<uint8_t>() & 3;
    changed = true;
  }
  if (!req["theme"].isNull()) {
    if (req["theme"].is<const char*>()) {
      s.theme = strcmp(req["theme"].as<const char*>(), "night") == 0 ? 1 : 0;
    } else {
      s.theme = req["theme"].as<int>() == 1 ? 1 : 0;
    }
    changed = true;
  }
  if (!req["dim_after_s"].isNull()) {
    s.dimAfterS = req["dim_after_s"].as<uint16_t>();
    changed = true;
  }
  if (!req["leds_muted"].isNull()) {
    s.ledsMuted = req["leds_muted"].as<bool>();
    changed = true;
  }
  if (changed) {
    applySettings();
    saveSettings();
  }
  JsonDocument resp;
  settingsJson(resp["data"].to<JsonObject>());
  hostSendResponse(resp);
}

void handleTracking(JsonDocument& req) {
  TrackingInfo& t = g_tracking;
  if (req["target"].is<const char*>()) t.target = req["target"].as<const char*>();
  if (!req["ra"].isNull()) t.ra = req["ra"].as<double>();
  if (!req["dec"].isNull()) t.dec = req["dec"].as<double>();
  if (!req["ra_now"].isNull()) t.raNow = req["ra_now"].as<double>();
  if (!req["dec_now"].isNull()) t.decNow = req["dec_now"].as<double>();
  if (!req["mag"].isNull()) t.mag = req["mag"].as<float>();
  if (!req["progress"].isNull()) t.progress = req["progress"].as<float>();
  if (req["state"].is<const char*>()) {
    String state = req["state"].as<const char*>();
    if (state == "tracking" && t.state != "tracking") {
      t.trackingSinceMs = millis();
      t.progress = -1.0f;
    }
    if (state != "tracking" && state != "centering" && state != "slewing") {
      t.trackingSinceMs = 0;
    }
    t.state = state;
  }
  bool active = t.state == "slewing" || t.state == "centering" || t.state == "tracking";
  if (active) {
    if (hostMode()) leaveHostMode();
    showTracking();
  } else if (trackingActive()) {
    showMenu();
  }
  invalidate();
}

}  // namespace

void hostPollPrompts() {
  if (g_releaseAtMs != 0 && static_cast<int32_t>(millis() - g_releaseAtMs) >= 0) {
    g_releaseAtMs = 0;
    injectKey(g_releaseKey, false);
  }
  if (g_hasPending && !dialogBusyLocally(g_pending.kind)) {
    g_hasPending = false;
    showPrompt(g_pending);
  }
  // The user may have left the dialog some other way (a panel stop).
  if (g_shownId.length() > 0 && !onStack(dialogFor(g_shownKind))) {
    g_shownId = "";
    g_shownKind = "";
  }
}

}  // namespace ui

void uiHandleCommand(JsonDocument& req) {
  using namespace ui;
  const char* action = req["action"] | "state";

  if (strcmp(action, "hello") == 0) {
    noteHello(req["device"] | static_cast<const char*>(nullptr));
    if (req["status"].is<JsonObjectConst>()) noteStatus(req["status"].as<JsonObjectConst>());
    if (req["panels"].is<JsonArrayConst>()) {
      String error;
      if (!setRemotePanels(req["panels"].as<JsonArrayConst>(), &error)) {
        hostSendError(error.c_str());
        return;
      }
    }
    JsonDocument resp;
    JsonObject data = resp["data"].to<JsonObject>();
    data["ui"] = 1;
    data["fw"] = SELENO_FW_VERSION;
    data["width"] = screenW();
    data["height"] = screenH();
    data["sky_w"] = skyViewWidth();
    data["sky_h"] = skyViewHeight();
    data["panel_stop_pending"] = panelStopPending();
    data["estop"] = estopLatched();
    Screen* s = top();
    data["screen"] = s != nullptr ? s->name() : "";
    hostSendResponse(resp);
    return;
  }
  if (strcmp(action, "status") == 0) {
    noteStatus(req.as<JsonObjectConst>());
    JsonDocument resp;
    JsonObject data = resp["data"].to<JsonObject>();
    data["panel_stop_pending"] = panelStopPending();
    data["estop"] = estopLatched();
    Screen* s = top();
    data["screen"] = s != nullptr ? s->name() : "";
    data["sky_w"] = skyViewWidth();
    data["sky_h"] = skyViewHeight();
    data["sky_shown"] = s == skyScreen();
    data["sky_radar"] = settings().skyRadar;
    hostSendResponse(resp);
    return;
  }
  if (strcmp(action, "sky") == 0) {
    uint32_t length = req["length"] | 0u;
    if (length < 4 || length > 32768) {
      hostSendError("sky needs \"length\": 4-32768 bytes of SKY1 scene");
      return;
    }
    BinaryTransfer t;
    t.kind = BinaryKind::SkyScene;
    t.total = length;
    if (!hostBeginBinary(t)) {
      hostSendError("Busy or out of memory");
      return;
    }
    return;  // answered once the bytes are in (uiSkySceneComplete)
  }
  if (strcmp(action, "menu") == 0) {
    String error;
    if (!setRemotePanels(req["panels"].as<JsonArrayConst>(), &error)) {
      hostSendError(error.c_str());
      return;
    }
    hostSendOk();
    return;
  }
  if (strcmp(action, "set") == 0) {
    int changed = 0;
    if (req["values"].is<JsonObjectConst>()) changed += setRemoteValues(req["values"].as<JsonObjectConst>());
    if (req["panel"].is<const char*>() && req["item"].is<const char*>()) {
      if (!updateRemoteItem(req["panel"], req["item"], req["fields"].as<JsonObjectConst>())) {
        hostSendError("Unknown Pi panel or item");
        return;
      }
      changed++;
    }
    JsonDocument resp;
    resp["data"]["changed"] = changed;
    hostSendResponse(resp);
    return;
  }
  if (strcmp(action, "prompt") == 0) {
    Prompt p;
    p.id = req["prompt"] | "";
    if (p.id.length() == 0) {
      hostSendError("prompt needs \"prompt\": an id for the answer");
      return;
    }
    p.kind = req["kind"] | "message";
    if (p.kind != "text" && p.kind != "confirm" && p.kind != "message" && p.kind != "busy" && p.kind != "choice" &&
        p.kind != "code") {
      hostSendError("kind must be text, confirm, message, busy, choice or code");
      return;
    }
    p.title = req["title"] | "";
    p.text = req["text"] | "";
    p.value = req["value"] | "";
    p.subtitle = req["subtitle"] | "";
    p.maxLength = req["max_length"] | 64;
    if (p.maxLength > 160) p.maxLength = 160;
    p.selected = req["selected"] | 0;
    p.yes = req["yes"] | "Yes";
    p.no = req["no"] | "No";
    p.defaultYes = strcmp(req["default"] | "no", "yes") == 0;
    p.timeoutMs = req["timeout_ms"] | 0u;
    const char* style = req["style"] | "";
    p.alert = strcmp(style, "danger") == 0 ? Alert::Danger : (strcmp(style, "warn") == 0 ? Alert::Warn : Alert::None);
    for (JsonVariantConst o : req["options"].as<JsonArrayConst>()) p.options.push_back(o.as<String>());
    if (p.kind == "choice" && p.options.empty()) {
      hostSendError("choice needs options");
      return;
    }
    bool queued = dialogBusyLocally(p.kind);
    if (queued) {
      g_pending = p;
      g_hasPending = true;
    } else {
      showPrompt(p);
    }
    JsonDocument resp;
    resp["data"]["queued"] = queued;
    hostSendResponse(resp);
    return;
  }
  if (strcmp(action, "close_prompt") == 0) {
    String id = req["prompt"] | "";
    if (g_hasPending && (id.length() == 0 || g_pending.id == id)) g_hasPending = false;
    if (g_shownId.length() > 0 && (id.length() == 0 || g_shownId == id)) dismissShown();
    hostSendOk();
    return;
  }
  if (strcmp(action, "toast") == 0) {
    if (hostMode()) leaveHostMode();
    toast(req["text"] | "", req["ms"] | 2000u);
    hostSendOk();
    return;
  }
  if (strcmp(action, "mode") == 0) {
    const char* mode = req["mode"] | "menu";
    leaveHostMode();
    if (strcmp(mode, "menu") == 0) {
      showMenu();
    } else if (strcmp(mode, "loading") == 0) {
      showLoading();
    } else if (strcmp(mode, "tracking") == 0 || strcmp(mode, "sky") == 0) {
      showTracking();
    } else {
      hostSendError("mode must be menu, tracking, sky or loading");
      return;
    }
    hostSendOk();
    return;
  }
  if (strcmp(action, "tracking") == 0) {
    handleTracking(req);
    hostSendOk();
    return;
  }
  if (strcmp(action, "loading") == 0) {
    setLoadingText(req["text"] | "");
    hostSendOk();
    return;
  }
  if (strcmp(action, "show") == 0) {
    leaveHostMode();
    if (!menuShowPanel(req["panel"] | "", req["item"] | "")) {
      hostSendError("Unknown panel");
      return;
    }
    hostSendOk();
    return;
  }
  if (strcmp(action, "key") == 0) {
    Key k;
    if (!parseKey(req["key"] | "", k)) {
      hostSendError("key must be select, return, up, down, left or right");
      return;
    }
    const char* how = req["press"] | "click";
    if (strcmp(how, "click") == 0) {
      injectKey(k, true);
      injectKey(k, false);
    } else if (strcmp(how, "down") == 0) {
      injectKey(k, true);
    } else if (strcmp(how, "up") == 0) {
      injectKey(k, false);
    } else if (strcmp(how, "long") == 0) {
      injectKey(k, true);
      g_releaseKey = k;
      g_releaseAtMs = millis() + kLongPressMs + 60;
    } else {
      hostSendError("press must be click, down, up or long");
      return;
    }
    hostSendOk();
    return;
  }
  if (strcmp(action, "ack_stop") == 0) {
    panelStopAcknowledge();
    invalidate();
    JsonDocument resp;
    resp["data"]["panel_stop_pending"] = false;
    hostSendResponse(resp);
    return;
  }
  if (strcmp(action, "settings") == 0) {
    handleSettings(req);
    return;
  }
  if (strcmp(action, "screenshot") == 0) {
    handleScreenshot();
    return;
  }
  if (strcmp(action, "state") == 0) {
    JsonDocument resp;
    addState(resp["data"].to<JsonObject>());
    hostSendResponse(resp);
    return;
  }
  hostSendError("Unknown ui action");
}
