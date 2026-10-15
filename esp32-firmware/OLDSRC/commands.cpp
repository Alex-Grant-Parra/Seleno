#include "commands.h"
#include "motor.h"
#include "led.h"
#include "display.h"
#include "serial_handler.h"
#include <ArduinoJson.h>
#include <LittleFS.h>

// Print a string as the body of a JSON string literal (no surrounding quotes).
static void printJsonEscaped(const char* str) {
  for (const char* p = str; *p != '\0'; p++) {
    char c = *p;
    if (c == '"' || c == '\\') {
      Serial.print('\\');
      Serial.print(c);
    } else if (static_cast<unsigned char>(c) < 0x20) {
      char esc[7];
      snprintf(esc, sizeof(esc), "\\u%04X", static_cast<unsigned char>(c));
      Serial.print(esc);
    } else {
      Serial.print(c);
    }
  }
}

void sendError(const char* message) {
  JsonDocument resp;
  resp["status"] = "error";
  resp["message"] = message;
  serializeJson(resp, Serial);
  Serial.println();
}

void sendOkEmpty() {
  JsonDocument resp;
  resp["status"] = "ok";
  serializeJson(resp, Serial);
  Serial.println();
}

void sendOkLedStatus(const LedChannel& led) {
  (void)led;
  sendOkEmpty();
}

void sendOkStatus(const String& motorId, Motor* motor) {
  // Keep the status payload intentionally small so it is less likely to be
  // truncated or corrupted over the serial link.
  Serial.print("{\"status\":\"ok\",\"data\":{");
  Serial.print("\"motor\":\"");
  printJsonEscaped(motorId.c_str());
  Serial.print("\",\"enabled\":");
  Serial.print(motor->isEnabled() ? "true" : "false");
  Serial.print(",\"moving\":");
  Serial.print(motor->isMoving() ? "true" : "false");
  Serial.print("}}\n");
}

void sendOkMotorList() {
  JsonDocument resp;
  resp["status"] = "ok";
  JsonObject data = resp["data"].to<JsonObject>();
  JsonArray motors = data["motors"].to<JsonArray>();
  for (size_t i = 0; i < kMaxMotors; i++) {
    if (g_motors[i].motor != nullptr) {
      motors.add(g_motors[i].id);
    }
  }
  serializeJson(resp, Serial);
  Serial.println();
}

void sendOkDisplayStatus() {
  JsonDocument resp;
  resp["status"] = "ok";
  JsonObject data = resp["data"].to<JsonObject>();
  data["initialized"] = g_display_state.initialized;
  data["powered"] = g_display_state.powered;
  data["brightness"] = g_display_state.brightness;
  data["width"] = DISPLAY_WIDTH;
  data["height"] = DISPLAY_HEIGHT;
  data["cursor_x"] = g_display_state.cursor_x;
  data["cursor_y"] = g_display_state.cursor_y;
  data["text_color"] = colorToHex(g_display_state.text_color);
  data["background_color"] = colorToHex(g_display_state.background_color);
  serializeJson(resp, Serial);
  Serial.println();
}

static void handleLedCommand(const JsonDocument& req) {
  const char* ledName = req["led"] | "board";
  LedChannel* led = findLed(String(ledName));
  if (led == nullptr) {
    sendError("Unknown LED");
    return;
  }

  if (!req["mode"].isNull()) {
    const char* mode = req["mode"] | "";
    if (strcmp(mode, "on") == 0) {
      applyLedState(led, true);
    } else if (strcmp(mode, "off") == 0) {
      applyLedState(led, false);
    } else if (strcmp(mode, "blink") == 0) {
      uint32_t intervalMs = req["interval_ms"] | kDefaultLedBlinkMs;
      uint32_t autoOffMs = req["auto_off_ms"] | 0;
      startLedBlink(led, intervalMs, autoOffMs);
    } else if (strcmp(mode, "toggle") == 0) {
      applyLedState(led, !led->state);
    } else {
      sendError("Invalid LED mode");
      return;
    }
    sendOkLedStatus(*led);
    return;
  }

  if (!req["on"].isNull()) {
    bool on = req["on"] | false;
    applyLedState(led, on);
    sendOkLedStatus(*led);
    return;
  }

  sendError("Missing LED mode");
}

// Return the frame delay at position index in a comma-separated list of
// milliseconds, or 100 ms if the entry is missing or empty. Negative values
// are treated as 0 rather than wrapping to a ~50 day delay.
static uint32_t frameDelayMs(const char* durations_csv, int index) {
  uint32_t delay_ms = 100;
  if (strlen(durations_csv) == 0) {
    return delay_ms;
  }
  String ds(durations_csv);
  int s2 = 0;
  for (int j = 0; j <= index; j++) {
    int c2 = ds.indexOf(',', s2);
    if (j == index) {
      String part = (c2 == -1) ? ds.substring(s2) : ds.substring(s2, c2);
      part.trim();
      if (part.length() > 0) {
        long v = part.toInt();
        delay_ms = v < 0 ? 0 : static_cast<uint32_t>(v);
      }
      break;
    }
    if (c2 == -1) break;
    s2 = c2 + 1;
  }
  return delay_ms;
}

static void handleDisplayCommand(const JsonDocument& req) {
  const char* action = req["action"] | "";
  (void)action; // avoid unused var warnings when debug is disabled
  if (strlen(action) == 0) {
    sendError("Missing display action");
    return;
  }

  if (strcmp(action, "init") == 0) {
    initializeDisplay();
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "power") == 0) {
    bool on = req["on"] | true;
    displayPower(on);
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "backlight") == 0) {
    uint8_t brightness = req["brightness"] | 255;
    displayBacklight(brightness);
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "clear") == 0) {
    uint16_t color = 0x0000;  // Black
    if (!req["color"].isNull()) {
      const char* colorHex = req["color"] | "000000";
      color = hexToColor(String(colorHex));
    }
    displayFillScreen(color);
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "fill_rect") == 0) {
    uint16_t x = req["x"] | 0;
    uint16_t y = req["y"] | 0;
    uint16_t w = req["w"] | 10;
    uint16_t h = req["h"] | 10;
    const char* colorHex = req["color"] | "FFFFFF";
    uint16_t color = hexToColor(String(colorHex));
    displayFillRectangle(x, y, w, h, color);
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "draw_rect") == 0) {
    uint16_t x = req["x"] | 0;
    uint16_t y = req["y"] | 0;
    uint16_t w = req["w"] | 10;
    uint16_t h = req["h"] | 10;
    const char* colorHex = req["color"] | "FFFFFF";
    uint16_t color = hexToColor(String(colorHex));
    displayDrawRectangle(x, y, w, h, color);
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "draw_line") == 0) {
    uint16_t x0 = req["x0"] | 0;
    uint16_t y0 = req["y0"] | 0;
    uint16_t x1 = req["x1"] | 127;
    uint16_t y1 = req["y1"] | 159;
    const char* colorHex = req["color"] | "FFFFFF";
    uint16_t color = hexToColor(String(colorHex));
    displayDrawLine(x0, y0, x1, y1, color);
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "draw_circle") == 0) {
    uint16_t x = req["x"] | 64;
    uint16_t y = req["y"] | 80;
    uint16_t r = req["r"] | 10;
    const char* colorHex = req["color"] | "FFFFFF";
    uint16_t color = hexToColor(String(colorHex));
    displayDrawCircle(x, y, r, color);
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "fill_circle") == 0) {
    uint16_t x = req["x"] | 64;
    uint16_t y = req["y"] | 80;
    uint16_t r = req["r"] | 10;
    const char* colorHex = req["color"] | "FFFFFF";
    uint16_t color = hexToColor(String(colorHex));
    displayFillCircle(x, y, r, color);
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "set_cursor") == 0) {
    uint16_t x = req["x"] | 0;
    uint16_t y = req["y"] | 0;
    displaySetCursor(x, y);
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "set_text_color") == 0) {
    const char* colorHex = req["color"] | "FFFFFF";
    uint16_t color = hexToColor(String(colorHex));
    displaySetTextColor(color);
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "set_bg_color") == 0) {
    const char* colorHex = req["color"] | "000000";
    uint16_t color = hexToColor(String(colorHex));
    displaySetBackgroundColor(color);
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "status") == 0) {
    sendOkDisplayStatus();
    return;
  }

  if (strcmp(action, "format_storage") == 0) {
    if (!LittleFS.begin(true)) {
      sendError("Storage mount failed");
      return;
    }
    if (!LittleFS.format()) {
      sendError("Storage format failed");
      return;
    }
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "storage_info") == 0) {
    if (!LittleFS.begin(true)) {
      sendError("Storage mount failed");
      return;
    }
    // Report total and free space — stream manually to avoid possible truncation
    size_t total = LittleFS.totalBytes();
    size_t used = LittleFS.usedBytes();
    uint64_t freeb = (uint64_t)(total >= used ? (total - used) : 0);

    Serial.print("{\"status\":\"ok\",\"data\":{\"total_bytes\":");
    Serial.print((uint64_t)total);
    Serial.print(",\"used_bytes\":");
    Serial.print((uint64_t)used);
    Serial.print(",\"free_bytes\":");
    Serial.print(freeb);
    Serial.print("}}\n");
    return;
  }

  if (strcmp(action, "list_files") == 0) {
    if (!LittleFS.begin(true)) {
      sendError("Storage mount failed");
      return;
    }
    // Stream JSON manually to avoid ArduinoJson capacity/truncation issues
    Serial.print("{\"status\":\"ok\",\"data\":{\"files\":[");
    bool first = true;
    File root = LittleFS.open("/");
    if (root) {
      File file = root.openNextFile();
      while (file) {
        if (!first) {
          Serial.print(',');
        }
        first = false;
        // Print object: {"name":"...","size":123}
        Serial.print('{');
        // name (escape quotes/backslashes)
        Serial.print("\"name\":\"");
        printJsonEscaped(file.name());
        Serial.print("\"");
        Serial.print(',');
        Serial.print("\"size\":");
        Serial.print((uint64_t)file.size());
        Serial.print('}');
        file = root.openNextFile();
      }
      root.close();
    }
    Serial.print("]}}\n");
    return;
  }

  if (strcmp(action, "delete_file") == 0) {
    const char* name = req["name"] | "";
    if (strlen(name) == 0) {
      sendError("Missing name");
      return;
    }
    if (!LittleFS.begin(true)) {
      sendError("Storage mount failed");
      return;
    }
    String fname(name == nullptr ? "" : name);
    fname.trim();
    if (fname.length() > 0 && fname[0] != '/') {
      fname = "/" + fname;
    }
    if (!LittleFS.exists(fname)) {
      sendError("File not found");
      return;
    }
    if (!LittleFS.remove(fname)) {
      sendError("Delete failed");
      return;
    }
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "blit") == 0) {
    uint16_t x = req["x"] | 0;
    uint16_t y = req["y"] | 0;
    uint16_t w = req["w"] | 0;
    uint16_t h = req["h"] | 0;
    const char* format = req["format"] | "RGB565";
    uint32_t length = req["length"] | 0;

    if (w == 0 || h == 0) {
      sendError("Invalid blit size");
      return;
    }

    // The payload is written straight into panel RAM and cannot be clipped.
    if (static_cast<uint32_t>(x) + w > DISPLAY_WIDTH || static_cast<uint32_t>(y) + h > DISPLAY_HEIGHT) {
      sendError("Blit out of bounds");
      return;
    }

    if (!g_display_state.initialized) {
      sendError("Display not initialized");
      return;
    }

    if (strcmp(format, "RGB565") != 0) {
      sendError("Unsupported blit format");
      return;
    }

    if (length != static_cast<uint32_t>(w) * static_cast<uint32_t>(h) * 2U) {
      sendError("Invalid blit length");
      return;
    }

    // Try buffered blit first (preferred) and fall back to streaming blit if
    // buffering isn't available or allocation fails.
    if (!beginDisplayBlitBuffered(x, y, w, h, length)) {
      if (!beginDisplayBlit(x, y, w, h, length)) {
        sendError("Unable to start display blit");
        return;
      }
    }

    // Response is sent after the binary payload is fully received.
    return;
  }

  if (strcmp(action, "store_begin") == 0) {
    const char* name = req["name"] | "";
    uint32_t length = req["length"] | 0;
    if (strlen(name) == 0 || length == 0) {
      sendError("Missing name or length");
      return;
    }

    if (!beginFileUpload(name, length)) {
      sendError("Unable to start file upload");
      return;
    }

    sendOkEmpty();
    return;
  }

  if (strcmp(action, "store") == 0) {
    const char* name = req["name"] | "";
    uint32_t length = req["length"] | 0;
    if (strlen(name) == 0 || length == 0) {
      sendError("Missing name or length");
      return;
    }

    if (!beginFileUpload(name, length)) {
      sendError("Unable to start file upload");
      return;
    }

    // Response will be sent after upload completes
    return;
  }

  if (strcmp(action, "play") == 0) {
    const char* name = req["name"] | "";
    if (strlen(name) == 0) {
      sendError("Missing name");
      return;
    }

    uint16_t x = req["x"] | 0;
    uint16_t y = req["y"] | 0;
    uint16_t w = req["w"] | DISPLAY_WIDTH;
    uint16_t h = req["h"] | DISPLAY_HEIGHT;

    displayPlayFile(name, x, y, w, h);
    sendOkEmpty();
    return;
  }

  if (strcmp(action, "play_sequence") == 0) {
    // Play a sequence of stored files on the device for smooth animation.
    const char* files_csv = req["files_csv"] | "";
    if (strlen(files_csv) == 0) {
      sendError("Missing files_csv");
      return;
    }

    const char* durations_csv = req["durations_csv"] | "";

    uint16_t x = req["x"] | 0;
    uint16_t y = req["y"] | 0;
    uint16_t w = req["w"] | DISPLAY_WIDTH;
    uint16_t h = req["h"] | DISPLAY_HEIGHT;

    // Ensure storage is mounted
    if (!LittleFS.begin(true)) {
      sendError("Storage mount failed");
      return;
    }

    // Split CSV file list
    String fileList(files_csv);
    int start = 0;
    int idx = 0;
    while (start < fileList.length()) {
      int comma = fileList.indexOf(',', start);
      String fname = (comma == -1) ? fileList.substring(start) : fileList.substring(start, comma);
      fname.trim();
      if (fname.length() > 0) {
        displayPlayFile(fname.c_str(), x, y, w, h);
        delay(frameDelayMs(durations_csv, idx));
        yield();
        idx++;
      }
      if (comma == -1) break;
      start = comma + 1;
    }

    sendOkEmpty();
    return;
  }

  if (strcmp(action, "play_sequence_prefix") == 0) {
    const char* prefix = req["prefix"] | "";
    int start = req["start"] | 0;
    int count = req["count"] | 0;
    int pad = req["pad"] | 4;
    const char* durations_csv = req["durations_csv"] | "";

    if (strlen(prefix) == 0 || count <= 0) {
      sendError("Missing prefix or count");
      return;
    }

    uint16_t x = req["x"] | 0;
    uint16_t y = req["y"] | 0;
    uint16_t w = req["w"] | DISPLAY_WIDTH;
    uint16_t h = req["h"] | DISPLAY_HEIGHT;

    if (!LittleFS.begin(true)) {
      sendError("Storage mount failed");
      return;
    }

    for (int i = start; i < start + count; i++) {
      char fname[128];
      // Build filename: prefix + zero-padded index + .rgb
      snprintf(fname, sizeof(fname), "%s%0*d.rgb", prefix, pad, i);
      displayPlayFile(fname, x, y, w, h);

      delay(frameDelayMs(durations_csv, i - start));
      yield();
    }

    sendOkEmpty();
    return;
  }

  sendError("Unknown display action");
}

// A motor pin must be an output-capable GPIO that isn't already in use.
static bool isUsableMotorPin(int pin) {
  if (pin < 0 || pin > 33) {
    return false;  // 34-39 are input-only
  }
  if (pin == 1 || pin == 3) {
    return false;  // UART0 TX/RX: the host serial link
  }
  if (pin >= 6 && pin <= 11) {
    return false;  // Connected to the SPI flash
  }
  if (pin == DISPLAY_SCK || pin == DISPLAY_SDA || pin == DISPLAY_DC ||
      pin == DISPLAY_RES || pin == DISPLAY_BL) {
    return false;
  }
  for (size_t i = 0; i < kLedCount; i++) {
    if (g_leds[i].pin == pin) {
      return false;
    }
  }
  for (size_t i = 0; i < kMaxMotors; i++) {
    Motor* m = g_motors[i].motor;
    if (m != nullptr && (m->stepPin == pin || m->dirPin == pin || m->enPin == pin)) {
      return false;
    }
  }
  return true;
}

static void handleMotorCreateCommand(const JsonDocument& req) {
  const char* motorId = req["motor"] | "";
  if (strlen(motorId) == 0) {
    sendError("Missing motor id");
    return;
  }
  if (findMotor(motorId) != nullptr) {
    sendError("Motor already exists");
    return;
  }
  if (req["step"].isNull() || req["dir"].isNull() || req["en"].isNull()) {
    sendError("Missing motor pins");
    return;
  }
  
  int stepPin = req["step"].as<int>();
  int dirPin = req["dir"].as<int>();
  int enPin = req["en"].as<int>();
  if (stepPin == dirPin || stepPin == enPin || dirPin == enPin ||
      !isUsableMotorPin(stepPin) || !isUsableMotorPin(dirPin) || !isUsableMotorPin(enPin)) {
    sendError("Invalid motor pins");
    return;
  }
  uint32_t stepsPerRev = req["steps_per_rev"] | STEPS_PER_REVOLUTION;
  
  Motor* motor = new Motor(stepPin, dirPin, enPin, stepsPerRev);
  motor->initialize();
  
  uint32_t speedUs = req["speed_us"] | motor->getStepDelayUs();
  if (speedUs <= PULSE_WIDTH_US) {
    speedUs = PULSE_WIDTH_US + 1;
  }
  motor->setSpeed(speedUs);
  
  bool engage = req["engage"] | false;
  if (engage) {
    motor->engage();
  }
  
  if (!registerMotor(motorId, motor, true)) {
    delete motor;
    sendError("Motor registry full");
    return;
  }
  sendOkEmpty();
}

static void handleMotorDeleteCommand(const JsonDocument& req) {
  const char* motorId = req["motor"] | "";
  if (strlen(motorId) == 0) {
    sendError("Missing motor id");
    return;
  }
  for (size_t i = 0; i < kMaxMotors; i++) {
    if (g_motors[i].motor != nullptr && g_motors[i].id == motorId) {
      g_motors[i].motor->cleanup();
      if (g_motors[i].owned) {
        delete g_motors[i].motor;
      }
      g_motors[i].motor = nullptr;
      g_motors[i].id = "";
      g_motors[i].owned = false;
      sendOkEmpty();
      return;
    }
  }
  sendError("Motor not found");
}

static void handleMotorMotionCommand(const String& cmd, const JsonDocument& req, Motor* motor) {
  if (cmd == "engage") {
    motor->engage();
    sendOkEmpty();
  } else if (cmd == "disengage") {
    motor->disengage();
    sendOkEmpty();
  } else if (cmd == "enable") {
    bool on = req["value"] | false;
    if (on) {
      motor->engage();
    } else {
      motor->disengage();
    }
    sendOkEmpty();
  } else if (cmd == "set_speed") {
    uint32_t speedUs = motor->getStepDelayUs();
    if (!req["speed_us"].isNull()) {
      speedUs = req["speed_us"].as<uint32_t>();
    } else if (!req["sps"].isNull()) {
      float sps = req["sps"].as<float>();
      if (sps <= 0.0f) {
        sendError("Invalid sps");
        return;
      }
      float delay = 1000000.0f / sps;
      if (delay < (PULSE_WIDTH_US + 1)) {
        delay = PULSE_WIDTH_US + 1;
      }
      speedUs = static_cast<uint32_t>(delay);
    } else {
      sendError("Missing speed value");
      return;
    }
    if (speedUs <= PULSE_WIDTH_US) {
      speedUs = PULSE_WIDTH_US + 1;
    }
    motor->setSpeed(speedUs);
    sendOkEmpty();
  } else if (cmd == "turn_degrees") {
    if (req["degrees"].isNull()) {
      sendError("Missing degrees");
      return;
    }
    float degrees = req["degrees"].as<float>();
    bool forward = req["forward"] | true;
    motor->turnDegrees(degrees, forward);
    sendOkEmpty();
  } else if (cmd == "start_continuous") {
    bool forward = req["forward"] | true;
    motor->startContinuous(forward);
    sendOkEmpty();
  } else if (cmd == "stop") {
    motor->stop();
    sendOkEmpty();
  } else if (cmd == "status") {
    String motorId = req["motor"] | "";
    sendOkStatus(motorId, motor);
  } else if (cmd == "get_position") {
    String motorId = req["motor"] | "";
    Serial.print("{\"status\":\"ok\",\"data\":{");
    Serial.print("\"motor\":\"");
    printJsonEscaped(motorId.c_str());
    Serial.print("\",\"position\":");
    Serial.print(motor->getPosition());
    Serial.print("}}\n");
  } else if (cmd == "reset_position") {
    motor->resetPosition();
    sendOkEmpty();
  } else {
    sendError("Unknown motor command");
  }
}

void handleCommand(const String& line) {
  if (line.length() == 0) {
    return;
  }

  // ArduinoJson 7 documents grow as needed, so no capacity is required.
  JsonDocument req;
  DeserializationError err = deserializeJson(req, line);
  if (err) {
    // Do not emit raw debug lines here as they contaminate JSON responses
    sendError("Invalid JSON");
    return;
  }

  const char* cmd = req["cmd"] | "";
  if (strlen(cmd) == 0) {
    sendError("Missing cmd");
    return;
  }

  // LED commands
  if (strcmp(cmd, "led") == 0) {
    handleLedCommand(req);
    return;
  }

  // Display commands
  if (strcmp(cmd, "display") == 0) {
    handleDisplayCommand(req);
    return;
  }

  // Motor creation/deletion commands
  if (strcmp(cmd, "create_motor") == 0) {
    handleMotorCreateCommand(req);
    return;
  }

  if (strcmp(cmd, "list_motors") == 0) {
    sendOkMotorList();
    return;
  }

  if (strcmp(cmd, "delete_motor") == 0) {
    handleMotorDeleteCommand(req);
    return;
  }

  // Motor operation commands (require valid motor_id)
  const char* motorId = req["motor"] | "";
  if (strlen(motorId) == 0) {
    sendError("Missing motor id");
    return;
  }
  
  Motor* motor = findMotor(motorId);
  if (motor == nullptr) {
    sendError("Unknown motor id");
    return;
  }

  handleMotorMotionCommand(cmd, req, motor);
}
