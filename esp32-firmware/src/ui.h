// Front-panel UI on the LCD, driven by the six buttons.
//
//  * Loading screen ("SELENO") from power-on until the Pi says hello.
//  * Menu: panels as tabs across the top, their items below. Panels come
//    from the firmware (ui_panels.cpp: motors, board tests, display, about)
//    and from the Pi ({"cmd":"ui","action":"menu"}: setup, scope, system).
//  * On-screen keyboard for text items and Pi prompts (first-run setup).
//  * Tracking screen while the Pi tracks; RETURN twice stops it.
//
// Runs in the main loop task: uiPoll() draws into a frame sprite and pushes
// it to the panel. The host can still draw directly with display commands;
// the UI steps aside until RETURN is pressed or the host hands back.
#pragma once

#include <ArduinoJson.h>

// After panelBegin() and storageBegin().
void uiBegin();
void uiPoll();

// {"cmd":"ui", ...} (see PROTOCOL.md).
void uiHandleCommand(JsonDocument& req);

// Display commands that draw call this first: the host owns the screen
// until RETURN or a ui command. Restores the host's rotation.
void uiHostDrawing();

// The LED test owns the LEDs; host LED commands are refused meanwhile.
bool uiLedTestActive();
