// Root screens and dialog internals shared by ui_host.cpp and ui_core.cpp.
#pragma once

#include <cmath>
#include <functional>

#include "ui_internal.h"

namespace ui {

// What the Pi says about the current target ({"cmd":"ui","action":"tracking"}).
struct TrackingInfo {
  String target;
  String state = "idle";  // slewing, centering, tracking, stopped, idle
  double ra = NAN;        // target, degrees
  double dec = NAN;
  double raNow = NAN;     // where the mount points now, degrees
  double decNow = NAN;
  float mag = NAN;
  float progress = -1.0f;        // 0..1 while slewing; < 0 hides the bar
  uint32_t trackingSinceMs = 0;  // when state became "tracking"
};
extern TrackingInfo g_tracking;

void setLoadingText(const String& text);
// A message that can show a spinner instead of an OK button (busy) and
// close itself after timeoutMs (0 = never).
void openBusyMessage(const String& title, const String& text, std::function<void()> closed, bool busy,
                     uint32_t timeoutMs);

Screen* keyboardScreen();
Screen* confirmScreen();
Screen* codeScreen();
Screen* messageScreen();
Screen* choiceScreen();
void dismissKeyboard();
void dismissConfirm();
void dismissCode();
void dismissMessage();
void dismissChoice();

// Called every uiPoll(): shows a pending tracking screen once the user is
// back at the menu.
void screensPoll();

}  // namespace ui
