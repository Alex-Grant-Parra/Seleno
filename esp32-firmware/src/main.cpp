// SELENO v0.8 controller firmware. See ../PROTOCOL.md for the host protocol
// and "SELENO Controller v0.8 Hardware Report.txt" for the hardware.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <esp_system.h>
#include <SPI.h>  // TEMP LCD raw test

#include "board_pins.h"
#include "config.h"
#include "estop.h"
#include "front_panel.h"
#include "host_link.h"
#include "i2c_bus.h"
#include "imu.h"
#include "motors.h"
#include "storage.h"
#include "tmc_drivers.h"
#include "ui.h"

// ArduinoJson documents, the display path and LittleFS share the loop
// task's stack; 8 KB is the default.
SET_LOOP_TASK_STACK_SIZE(16 * 1024);

void setup() {
  hostBegin();

#ifdef SELENO_LCD_RAW_TEST
  // TEMP bring-up test: drive the ST7735S with plain Arduino SPI and the old
  // driver's init (no LovyanGFX), then cycle red/green/blue/black forever.
  // Nothing else starts. Build with -DSELENO_LCD_RAW_TEST=1; remove once the
  // LCD works.
  {
    // Hold both drivers off and STEP/DIR low: left floating, they pick up
    // the SPI bursts and a driver can take steps.
    for (int pin : {PIN_M1_EN, PIN_M2_EN}) { pinMode(pin, OUTPUT); digitalWrite(pin, HIGH); }
    for (int pin : {PIN_M1_STEP, PIN_M1_DIR, PIN_M2_STEP, PIN_M2_DIR}) { pinMode(pin, OUTPUT); digitalWrite(pin, LOW); }
    static SPIClass lcdSpi(HSPI);
    auto cmd = [](uint8_t c) { digitalWrite(PIN_LCD_DC, LOW); lcdSpi.write(c); digitalWrite(PIN_LCD_DC, HIGH); };
    auto dat = [](uint8_t d) { lcdSpi.write(d); };
    pinMode(PIN_LCD_DC, OUTPUT);
    pinMode(PIN_LCD_RST, OUTPUT);
    pinMode(PIN_LCD_BL, OUTPUT);
    digitalWrite(PIN_LCD_BL, HIGH);
    lcdSpi.begin(PIN_LCD_SCK, -1, PIN_LCD_MOSI, -1);
    lcdSpi.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE0));
    digitalWrite(PIN_LCD_RST, HIGH); delay(5);
    digitalWrite(PIN_LCD_RST, LOW); delay(20);
    digitalWrite(PIN_LCD_RST, HIGH); delay(150);
    cmd(0x01); delay(150);            // SWRESET
    cmd(0x11); delay(255);            // SLPOUT
    cmd(0x3A); dat(0x05);             // COLMOD 16-bit
    cmd(0x36); dat(0x08);             // MADCTL BGR
    cmd(0x29); delay(20);             // DISPON
    const uint16_t colors[] = {0xF800, 0x07E0, 0x001F, 0x0000};
    for (int i = 0;; i = (i + 1) % 4) {
      cmd(0x2A); dat(0); dat(0); dat(0); dat(127);   // CASET 0-127
      cmd(0x2B); dat(0); dat(0); dat(0); dat(159);   // RASET 0-159
      cmd(0x2C);                                      // RAMWR
      for (int p = 0; p < 128 * 160; p++) { dat(colors[i] >> 8); dat(colors[i] & 0xFF); }
      delay(1500);
    }
  }
#endif

  // GPIO5 is a strap with an internal pull-up: hold the LCD backlight off
  // until the panel is initialised and cleared.
  pinMode(PIN_LCD_BL, OUTPUT);
  digitalWrite(PIN_LCD_BL, LOW);

  // Before anything else touches Wire (see i2c_bus.cpp).
  i2cBegin();
  storageBegin();

  // Drivers first: ENN high, then configure over UART. Then the step timer.
  driversBegin();
  motorsBegin();

  panelBegin();
  imuBegin();
  // Loading screen until the Pi says hello (see ui.h).
  uiBegin();

  JsonDocument ev;
  ev["event"] = "boot";
  ev["fw"] = SELENO_FW_VERSION;
  ev["board"] = SELENO_BOARD_NAME;
  ev["devkit"] = SELENO_DEVKIT != 0;
  ev["reset_reason"] = static_cast<int>(esp_reset_reason());
  JsonArray drivers = ev["drivers"].to<JsonArray>();
  for (int i = 0; i < kAxisCount; i++) {
    drivers.add(driverPresent(i));
  }
  ev["expander"] = panelExpanderPresent();
  ev["ui"] = true;
  hostEmitEvent(ev);
  hostFlushEvents();
}

void loop() {
  hostPoll();
  driversPoll();
  motorsPoll();
  hostSilencePoll(millis());
  uiPoll();
  hostFlushEvents();
  // Let the idle task run (task watchdog) without adding real latency.
  delay(1);
}
