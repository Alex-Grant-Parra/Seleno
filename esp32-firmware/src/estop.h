// Emergency stop latch. Triggering stops all step generation at once and
// blocks new motion until the host clears it. The drivers stay energised so
// the mount holds position instead of swinging free.
#pragma once

#include <cstdint>

// Safe from any task. source is a short tag for the event ("button", "host").
void estopTrigger(const char* source);
void estopClear();
bool estopLatched();
const char* estopSource();

// Panel stop: RETURN pressed twice while the host is tracking. Stops every
// motor, holds new host moves (motorsSetHostHold) and tells the host with
// {"event":"ui","type":"stop"}; the hold lasts until the host answers
// {"cmd":"ui","action":"ack_stop"}. Needs no host. Safe from any task.
void panelStopTrigger(const char* reason);
bool panelStopPending();
void panelStopAcknowledge();
// Bumped on every panel stop, so the UI can notice one.
uint32_t panelStopCount();

// RETURN button policy, called on the first low reading of each press (and
// for keys the host injects):
//  * a host move faster than kReturnEstopAboveFsps (a slew): emergency stop;
//  * else a second press within kReturnDoublePressMs, while a host move runs
//    and the UI isn't using RETURN as its back key: panel stop.
// Otherwise RETURN is just a key for the UI.
void returnButtonPressed(uint32_t nowMs);
// The UI says whether RETURN is its back key right now (menus) or not
// (tracking screen, loading screen, host display).
void returnButtonSetNavigates(bool navigates);
bool returnButtonNavigates();
