// The sky view: where the telescope points, drawn like the server's star
// map. The Pi does the astronomy (catalogue from its server, stereographic
// projection centred on the scope) and sends a ready-projected scene; this
// side draws it with the overlays that need the motors or must react at
// once (reticle, target lock rings, axis arcs, radar inset, text strip).
//
// Scene: {"cmd":"ui","action":"sky","length":N} followed by N bytes in the
// "SKY1" format described in PROTOCOL.md.
#pragma once

#include "host_link.h"
#include "ui_internal.h"

namespace ui {

// The area the Pi should project for: the screen minus the top strip and
// the footer. Changes with the UI rotation.
int16_t skyViewWidth();
int16_t skyViewHeight();

// Parse a received scene and make it current. False with *error on a bad one.
bool skyAcceptScene(const uint8_t* data, size_t len, String* error);

// The tracking / sky screen (root screen while the Pi tracks).
Screen* skyScreen();
void skyScreenReset();

}  // namespace ui

// From onBinaryComplete (commands_display.cpp).
void uiSkySceneComplete(BinaryTransfer& t);
