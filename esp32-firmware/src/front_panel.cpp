#include "front_panel.h"

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include "board_pins.h"
#include "config.h"
#include "estop.h"
#include "host_link.h"
#include "i2c_bus.h"
#include "logic/debounce.h"
#include "logic/led_logic.h"

namespace {

// MCP23017 registers, IOCON.BANK = 0 (report, "Suggested start-up register writes")
constexpr uint8_t MCP_IODIRA = 0x00;
constexpr uint8_t MCP_IODIRB = 0x01;
constexpr uint8_t MCP_GPINTENA = 0x04;
constexpr uint8_t MCP_GPINTENB = 0x05;
constexpr uint8_t MCP_INTCONA = 0x08;
constexpr uint8_t MCP_INTCONB = 0x09;
constexpr uint8_t MCP_IOCON = 0x0A;
constexpr uint8_t MCP_GPPUA = 0x0C;
constexpr uint8_t MCP_GPPUB = 0x0D;
constexpr uint8_t MCP_INTCAPA = 0x10;
constexpr uint8_t MCP_GPIOA = 0x12;
constexpr uint8_t MCP_OLATA = 0x14;
constexpr uint8_t MCP_OLATB = 0x15;

constexpr uint8_t kIocon = 0x44;    // MIRROR = 1 (INTA covers both ports), ODR = 1
constexpr uint8_t kIodirA = 0x7C;   // GPA2-6 in; GPA0, GPA1, GPA7 out
constexpr uint8_t kIodirB = 0x1F;   // GPB0-4 in; GPB5-7 out
constexpr uint8_t kGppuA = 0x7C;    // pull-ups on every input, spares included
constexpr uint8_t kGppuB = 0x1F;
constexpr uint8_t kGpintenA = 0x04; // SELECT
constexpr uint8_t kGpintenB = 0x1F; // RETURN, TOP, BOTTOM, LEFT, RIGHT

constexpr uint32_t kHoldEventMs = 1000;
constexpr uint32_t kExpanderCheckMs = 2000;
constexpr uint32_t kExpanderReprobeMs = 3000;

// 16-bit sample: bits 0-7 = GPIOA, bits 8-15 = GPIOB.
struct ButtonDef {
  Button button;
  uint8_t bit;
};
constexpr ButtonDef kButtons[] = {
    {Button::Select, MCP_A_SELECT},
    {Button::Return, static_cast<uint8_t>(8 + MCP_B_RETURN)},
    {Button::Top, static_cast<uint8_t>(8 + MCP_B_TOP)},
    {Button::Bottom, static_cast<uint8_t>(8 + MCP_B_BOTTOM)},
    {Button::Left, static_cast<uint8_t>(8 + MCP_B_LEFT)},
    {Button::Right, static_cast<uint8_t>(8 + MCP_B_RIGHT)},
};
constexpr uint16_t kButtonMask = (1u << MCP_A_SELECT) | (0x1Fu << 8);
constexpr uint16_t kReturnBit = 1u << (8 + MCP_B_RETURN);

enum class LedSink : uint8_t { Gpio, PortA, PortB };
struct LedDef {
  const char* name;
  LedSink sink;
  uint8_t bitOrPin;
};
constexpr LedDef kLeds[kLedCount] = {
    {"board", LedSink::Gpio, PIN_BOARD_LED},
    {"yellow", LedSink::PortA, MCP_A_YELLOW},
    {"blue", LedSink::PortA, MCP_A_BLUE},
    {"white", LedSink::PortB, MCP_B_WHITE},
    {"green", LedSink::PortB, MCP_B_GREEN},
    {"red", LedSink::PortB, MCP_B_RED},
};

TaskHandle_t g_task = nullptr;
portMUX_TYPE g_ledMux = portMUX_INITIALIZER_UNLOCKED;
seleno::LedLogic g_ledLogic[kLedCount];

volatile bool g_present = false;
volatile uint16_t g_pressed = 0;  // debounced, bit per Button
volatile uint16_t g_rawSample = 0xFFFF;
volatile uint32_t g_interrupts = 0;
volatile uint32_t g_expanderResets = 0;
bool g_isrAttached = false;
volatile bool g_reinitRequested = false;

seleno::MaskDebouncer g_debouncer(kButtonMask, kButtonDebounceMs);
uint32_t g_pressStartMs[static_cast<int>(Button::Count)] = {0};
bool g_holdSent[static_cast<int>(Button::Count)] = {false};
bool g_returnStopFired = false;
uint32_t g_returnLastLowMs = 0;
volatile ButtonListener g_listener = nullptr;
volatile bool g_ledsMuted = false;
int16_t g_writtenOlatA = -1;
int16_t g_writtenOlatB = -1;
bool g_boardLedLevel = false;

void IRAM_ATTR onExpanderInterrupt() {
  g_interrupts++;
  BaseType_t woken = pdFALSE;
  if (g_task != nullptr) {
    vTaskNotifyGiveFromISR(g_task, &woken);
  }
  if (woken) {
    portYIELD_FROM_ISR();
  }
}

bool configureExpander() {
  if (!i2cProbe(MCP23017_ADDR)) {
    return false;
  }
  bool ok = i2cWriteReg8(MCP23017_ADDR, MCP_IOCON, kIocon) &&
            i2cWriteReg8(MCP23017_ADDR, MCP_OLATA, 0x00) &&
            i2cWriteReg8(MCP23017_ADDR, MCP_OLATB, 0x00) &&
            i2cWriteReg8(MCP23017_ADDR, MCP_IODIRA, kIodirA) &&
            i2cWriteReg8(MCP23017_ADDR, MCP_IODIRB, kIodirB) &&
            i2cWriteReg8(MCP23017_ADDR, MCP_GPPUA, kGppuA) &&
            i2cWriteReg8(MCP23017_ADDR, MCP_GPPUB, kGppuB) &&
            i2cWriteReg8(MCP23017_ADDR, MCP_INTCONA, 0x00) &&
            i2cWriteReg8(MCP23017_ADDR, MCP_INTCONB, 0x00) &&
            i2cWriteReg8(MCP23017_ADDR, MCP_GPINTENA, kGpintenA) &&
            i2cWriteReg8(MCP23017_ADDR, MCP_GPINTENB, kGpintenB);
  if (!ok) {
    return false;
  }
  uint8_t check[2] = {0, 0};
  if (!i2cReadRegs(MCP23017_ADDR, MCP_IODIRA, check, 2) || check[0] != kIodirA || check[1] != kIodirB) {
    return false;
  }
  uint8_t cap[2];
  i2cReadRegs(MCP23017_ADDR, MCP_INTCAPA, cap, 2);  // clear anything pending
  g_writtenOlatA = 0;
  g_writtenOlatB = 0;
  return true;
}

// A power glitch on U1 resets it to all-inputs with no pull-ups, which looks
// like dark LEDs and dead buttons. Check the direction registers now and then.
bool expanderStillConfigured() {
  uint8_t regs[2] = {0, 0};
  if (!i2cReadRegs(MCP23017_ADDR, MCP_IODIRA, regs, 2)) {
    return false;
  }
  uint8_t iocon = 0;
  if (!i2cReadRegs(MCP23017_ADDR, MCP_IOCON, &iocon, 1)) {
    return false;
  }
  return regs[0] == kIodirA && regs[1] == kIodirB && (iocon & kIocon) == kIocon;
}

void setPresent(bool present) {
  g_present = present;
  if (present && !g_isrAttached) {
    // IO39 has no internal pull-up; R38 provides it. On the bare dev kit
    // it floats, which is why the ISR is only attached once U1 answers.
    pinMode(PIN_EXP_INT, INPUT);
    attachInterrupt(PIN_EXP_INT, onExpanderInterrupt, FALLING);
    g_isrAttached = true;
  } else if (!present && g_isrAttached) {
    detachInterrupt(PIN_EXP_INT);
    g_isrAttached = false;
  }
}

uint16_t pressedFromRaw(uint16_t raw) {
  return static_cast<uint16_t>(~raw) & kButtonMask;  // buttons pull to GND
}

void emitButtonEvent(Button b, const char* action, uint32_t heldMs, bool includeHeld) {
  JsonDocument ev;
  ev["event"] = "button";
  ev["button"] = buttonName(b);
  ev["action"] = action;
  if (includeHeld) {
    ev["held_ms"] = heldMs;
  }
  ev["t"] = millis();
  hostEmitEvent(ev);
}

uint16_t buttonMaskFromSample(uint16_t pressedSample) {
  uint16_t mask = 0;
  for (const ButtonDef& def : kButtons) {
    if (pressedSample & (1u << def.bit)) {
      mask |= 1u << static_cast<int>(def.button);
    }
  }
  return mask;
}

void handleButtons(uint32_t now) {
  uint8_t gpio[2];
  if (!i2cReadRegs(MCP23017_ADDR, MCP_GPIOA, gpio, 2)) {
    return;
  }
  uint16_t raw = static_cast<uint16_t>(gpio[0]) | (static_cast<uint16_t>(gpio[1]) << 8);
  g_rawSample = raw;
  uint16_t pressedSample = pressedFromRaw(raw);

  // The RETURN stop policy acts on the first low reading, before
  // debouncing: a bouncing contact is still a press. The INT glitch erratum
  // can't fake this, because the level comes from the expander's GPIO
  // register.
  if (pressedSample & kReturnBit) {
    g_returnLastLowMs = now;
    if (!g_returnStopFired) {
      g_returnStopFired = true;
      returnButtonPressed(now);
    }
  } else if (g_returnStopFired && now - g_returnLastLowMs >= 2 * kButtonDebounceMs) {
    // Re-arm only once the contact has stayed open, so bounce on one press
    // doesn't fire a burst of stops.
    g_returnStopFired = false;
  }

  uint16_t changed = g_debouncer.update(pressedSample, now);
  uint16_t stable = g_debouncer.stable();
  g_pressed = buttonMaskFromSample(stable);

  for (const ButtonDef& def : kButtons) {
    int idx = static_cast<int>(def.button);
    uint16_t bit = 1u << def.bit;
    if (changed & bit) {
      bool pressed = (stable & bit) != 0;
      ButtonListener listener = g_listener;
      if (listener != nullptr) {
        listener(def.button, pressed, now);
      }
      if (pressed) {
        g_pressStartMs[idx] = now;
        g_holdSent[idx] = false;
        emitButtonEvent(def.button, "press", 0, false);
      } else {
        emitButtonEvent(def.button, "release", now - g_pressStartMs[idx], true);
      }
    } else if ((stable & bit) && !g_holdSent[idx] && now - g_pressStartMs[idx] >= kHoldEventMs) {
      g_holdSent[idx] = true;
      emitButtonEvent(def.button, "hold", now - g_pressStartMs[idx], true);
    }
  }
}

void applyLeds(uint32_t now, bool force) {
  uint8_t olatA = 0;
  uint8_t olatB = 0;
  bool board = false;
  portENTER_CRITICAL(&g_ledMux);
  for (int i = 0; i < kLedCount; i++) {
    bool on = g_ledLogic[i].update(now);
    switch (kLeds[i].sink) {
      case LedSink::Gpio: board = on; break;
      case LedSink::PortA: if (on) olatA |= 1u << kLeds[i].bitOrPin; break;
      case LedSink::PortB: if (on) olatB |= 1u << kLeds[i].bitOrPin; break;
    }
  }
  portEXIT_CRITICAL(&g_ledMux);
  if (g_ledsMuted) {
    olatA = 0;
    olatB = 0;
    board = false;
  }

  if (board != g_boardLedLevel || force) {
    digitalWrite(PIN_BOARD_LED, board ? HIGH : LOW);
    g_boardLedLevel = board;
  }
  if (!g_present) {
    return;
  }
  if (force || olatA != g_writtenOlatA) {
    if (i2cWriteReg8(MCP23017_ADDR, MCP_OLATA, olatA)) g_writtenOlatA = olatA;
  }
  if (force || olatB != g_writtenOlatB) {
    if (i2cWriteReg8(MCP23017_ADDR, MCP_OLATB, olatB)) g_writtenOlatB = olatB;
  }
}

void emitExpanderEvent(const char* name) {
  JsonDocument ev;
  ev["event"] = name;
  ev["device"] = "mcp23017";
  hostEmitEvent(ev);
}

void ioTask(void*) {
  uint32_t lastButtonRead = 0;
  uint32_t lastCheck = millis();
  uint32_t lastReprobe = millis();

  for (;;) {
    bool notified = ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(kIoTaskTickMs)) > 0;
    uint32_t now = millis();
    bool forceLeds = false;

    if (g_reinitRequested) {
      g_reinitRequested = false;
      bool ok = configureExpander();
      setPresent(ok);
      g_debouncer.reset(0, now);
      forceLeds = ok;
      lastReprobe = now;
      lastCheck = now;
    } else if (!g_present && now - lastReprobe >= kExpanderReprobeMs) {
      lastReprobe = now;
      if (configureExpander()) {
        setPresent(true);
        g_debouncer.reset(0, now);
        forceLeds = true;
        emitExpanderEvent("expander_found");
      }
    }

    if (g_present) {
      if (now - lastCheck >= kExpanderCheckMs) {
        lastCheck = now;
        if (!expanderStillConfigured()) {
          g_expanderResets++;
          if (configureExpander()) {
            forceLeds = true;
            emitExpanderEvent("expander_reset");
          } else {
            setPresent(false);
            lastReprobe = now;
            emitExpanderEvent("expander_lost");
          }
        }
      }
      if (g_present && (notified || g_debouncer.pending() || g_pressed != 0 || g_returnStopFired ||
                        now - lastButtonRead >= kButtonBackupPollMs)) {
        lastButtonRead = now;
        handleButtons(now);
      }
    }

    applyLeds(now, forceLeds);
  }
}

bool validLed(int led) {
  return led >= 0 && led < kLedCount;
}

}  // namespace

const char* buttonName(Button b) {
  switch (b) {
    case Button::Select: return "select";
    case Button::Return: return "return";
    case Button::Top: return "top";
    case Button::Bottom: return "bottom";
    case Button::Left: return "left";
    case Button::Right: return "right";
    default: return "unknown";
  }
}

void panelBegin() {
  pinMode(PIN_BOARD_LED, OUTPUT);
  digitalWrite(PIN_BOARD_LED, LOW);

  // U1's RESET RC needs about 200 ms after power-up.
  while (millis() < kExpanderPowerUpMs) {
    delay(5);
  }
  bool present = configureExpander();
  setPresent(present);
  g_debouncer.reset(0, millis());

  // Core 0, above the IMU task: the RETURN emergency stop must not wait.
  xTaskCreatePinnedToCore(ioTask, "panel", 4096, nullptr, 5, &g_task, 0);
}

bool panelExpanderPresent() {
  return g_present;
}

bool panelReinit() {
  // The panel task owns the expander; ask it and wait for the answer.
  g_reinitRequested = true;
  xTaskNotifyGive(g_task);
  uint32_t start = millis();
  while (g_reinitRequested && millis() - start < 200) {
    delay(2);
  }
  return g_present;
}

uint16_t panelPressedButtons() {
  return g_pressed;
}

void panelSetButtonListener(ButtonListener listener) {
  g_listener = listener;
}

void panelStatusJson(JsonObject out) {
  out["expander_present"] = g_present;
  out["address"] = MCP23017_ADDR;
  out["int_pin_low"] = g_present ? (digitalRead(PIN_EXP_INT) == LOW) : false;
  out["interrupts"] = g_interrupts;
  out["expander_resets"] = g_expanderResets;
  out["raw_gpio"] = g_rawSample;
  JsonObject buttons = out["buttons"].to<JsonObject>();
  uint16_t pressed = g_pressed;
  for (int i = 0; i < static_cast<int>(Button::Count); i++) {
    buttons[buttonName(static_cast<Button>(i))] = (pressed & (1u << i)) != 0;
  }
  out["leds_muted"] = static_cast<bool>(g_ledsMuted);
  JsonObject leds = out["leds"].to<JsonObject>();
  for (int i = 0; i < kLedCount; i++) {
    leds[kLeds[i].name] = ledState(i);
  }
}

int ledFind(const char* name) {
  if (name == nullptr) {
    return -1;
  }
  for (int i = 0; i < kLedCount; i++) {
    if (strcmp(kLeds[i].name, name) == 0) {
      return i;
    }
  }
  return -1;
}

const char* ledName(int led) {
  return validLed(led) ? kLeds[led].name : "";
}

void ledSet(int led, bool on) {
  if (!validLed(led)) return;
  portENTER_CRITICAL(&g_ledMux);
  g_ledLogic[led].set(on);
  portEXIT_CRITICAL(&g_ledMux);
}

void ledToggle(int led) {
  if (!validLed(led)) return;
  portENTER_CRITICAL(&g_ledMux);
  g_ledLogic[led].set(!g_ledLogic[led].state);
  portEXIT_CRITICAL(&g_ledMux);
}

void ledBlink(int led, uint32_t intervalMs, uint32_t autoOffMs) {
  if (!validLed(led)) return;
  uint32_t now = millis();
  portENTER_CRITICAL(&g_ledMux);
  g_ledLogic[led].startBlink(intervalMs, autoOffMs, now);
  portEXIT_CRITICAL(&g_ledMux);
}

bool ledState(int led) {
  if (!validLed(led)) return false;
  portENTER_CRITICAL(&g_ledMux);
  bool s = g_ledLogic[led].state;
  portEXIT_CRITICAL(&g_ledMux);
  return s;
}

void ledsSetMuted(bool muted) {
  g_ledsMuted = muted;
}

bool ledsMuted() {
  return g_ledsMuted;
}
