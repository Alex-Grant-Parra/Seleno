// Pop-up screens shared by the menu, the tests and Pi prompts. Each pushes
// itself on the screen stack; the callback runs after it has closed.
#pragma once

#include <functional>
#include <vector>

#include "ui_internal.h"

namespace ui {

// On-screen keyboard (ui_keyboard.cpp). done() gets the text on OK;
// cancelled() runs on RETURN.
void openKeyboard(const String& title, const String& value, uint16_t maxLength,
                  std::function<void(const String&)> done, std::function<void()> cancelled = nullptr,
                  const String& subtitle = "");

// The safe answer (no) is selected first unless defaultYes.
void openConfirm(const String& title, const String& text, std::function<void(bool)> result,
                 const char* yes = "Yes", const char* no = "No", bool defaultYes = false);
void openMessage(const String& title, const String& text, std::function<void()> closed = nullptr);
void openChoiceList(const String& title, const std::vector<String>& options, int selected,
                    std::function<void(int)> done, std::function<void()> cancelled = nullptr);

}  // namespace ui
