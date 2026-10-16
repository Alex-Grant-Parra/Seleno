// JSON command handlers. The protocol is documented in ../PROTOCOL.md.
#pragma once

#include <ArduinoJson.h>

// Each returns true if it recognised cmd (and sent a response).
bool handleMotorCommand(const char* cmd, JsonDocument& req);
bool handleDisplayCommand(const char* cmd, JsonDocument& req);
