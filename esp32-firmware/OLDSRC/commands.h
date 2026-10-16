#ifndef COMMANDS_H
#define COMMANDS_H

#include <Arduino.h>

// Command handling and JSON response functions
void handleCommand(const String& line);

// JSON response helpers
void sendError(const char* message);
void sendOkEmpty();
void sendOkLedStatus(const class LedChannel& led);
void sendOkStatus(const String& motorId, class Motor* motor);
void sendOkMotorList();
void sendOkDisplayStatus();

#endif // COMMANDS_H
