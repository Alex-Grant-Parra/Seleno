// Emergency stop latch. Triggering stops all step generation at once and
// blocks new motion until the host clears it. The drivers stay energised so
// the mount holds position instead of swinging free.
#pragma once

// Safe from any task. source is a short tag for the event ("button", "host").
void estopTrigger(const char* source);
void estopClear();
bool estopLatched();
const char* estopSource();
