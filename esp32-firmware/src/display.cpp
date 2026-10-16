#include "display.h"

#define LGFX_USE_V1
#include <LovyanGFX.hpp>
#include <LittleFS.h>

#include "board_pins.h"
#include "config.h"
#include "logic/color.h"
#include "storage.h"

namespace {

// Rotation 0 reproduces the old driver's CASET 0-127 / RASET 0-159 window.
// The controller's frame memory is 132 x 162; LovyanGFX uses that to work out
// offsets for the other rotations.
constexpr uint16_t kLcdOffsetX = 0;
constexpr uint16_t kLcdOffsetY = 0;
constexpr bool kLcdRgbOrder = false;  // false = BGR, as MADCTL 0x08 before

class SelenoLcd : public lgfx::LGFX_Device {
 public:
  SelenoLcd() {
    {
      auto cfg = bus_.config();
      cfg.spi_host = SPI2_HOST;  // HSPI: IO14/IO13 are its IO_MUX pins
      cfg.spi_mode = 0;
      cfg.freq_write = kLcdSpiHz;
      cfg.freq_read = 8000000;
      cfg.spi_3wire = true;
      cfg.use_lock = true;
      cfg.dma_channel = SPI_DMA_CH_AUTO;
      cfg.pin_sclk = PIN_LCD_SCK;
      cfg.pin_mosi = PIN_LCD_MOSI;
      cfg.pin_miso = -1;
      cfg.pin_dc = PIN_LCD_DC;
      bus_.config(cfg);
      panel_.setBus(&bus_);
    }
    {
      auto cfg = panel_.config();
      cfg.pin_cs = PIN_LCD_CS;  // tied to GND on S1 pin 7
      cfg.pin_rst = PIN_LCD_RST;
      cfg.pin_busy = -1;
      cfg.panel_width = kLcdWidth;
      cfg.panel_height = kLcdHeight;
      cfg.memory_width = 132;
      cfg.memory_height = 162;
      cfg.offset_x = kLcdOffsetX;
      cfg.offset_y = kLcdOffsetY;
      cfg.offset_rotation = 0;
      cfg.readable = false;  // no MISO on S1
      cfg.invert = false;
      cfg.rgb_order = kLcdRgbOrder;
      cfg.dlen_16bit = false;
      cfg.bus_shared = false;
      panel_.config(cfg);
    }
    {
      auto cfg = light_.config();
      cfg.pin_bl = PIN_LCD_BL;
      cfg.invert = false;
      cfg.freq = 12000;
      cfg.pwm_channel = kLcdBacklightPwmChannel;
      light_.config(cfg);
      panel_.setLight(&light_);
    }
    setPanel(&panel_);
  }

 private:
  lgfx::Panel_ST7735S panel_;
  lgfx::Bus_SPI bus_;
  lgfx::Light_PWM light_;
};

SelenoLcd g_lcd;

struct DisplayState {
  bool initialized = false;
  bool powered = true;
  uint8_t brightness = 255;
  uint16_t textColor = 0xFFFF;
  uint16_t backgroundColor = 0x0000;
  uint8_t textSize = 1;
  uint8_t rotation = 0;
  bool streaming = false;
  bool havePendingByte = false;
  uint8_t pendingByte = 0;
} g_state;

bool windowFits(uint32_t x, uint32_t y, uint32_t w, uint32_t h) {
  return w > 0 && h > 0 && x + w <= g_lcd.width() && y + h <= g_lcd.height();
}

}  // namespace

bool displayInit() {
  if (g_state.initialized) {
    return true;
  }
  // Keep the backlight dark until the panel RAM has been cleared, so the
  // random power-up contents never show.
  g_lcd.setBrightness(0);
  if (!g_lcd.init()) {
    return false;
  }
  g_lcd.setRotation(g_state.rotation);
  g_lcd.setColorDepth(16);
  g_lcd.fillScreen(0x0000);
  g_lcd.setTextColor(g_state.textColor, g_state.backgroundColor);
  g_lcd.setTextSize(g_state.textSize);
  g_lcd.setCursor(0, 0);
  g_state.initialized = true;
  g_state.powered = true;
  g_lcd.setBrightness(g_state.brightness);
  return true;
}

bool displayInitialized() {
  return g_state.initialized;
}

void displayPower(bool on) {
  if (!g_state.initialized) return;
  if (on) {
    g_lcd.wakeup();
  } else {
    g_lcd.sleep();
  }
  g_state.powered = on;
}

void displayBacklight(uint8_t brightness) {
  g_state.brightness = brightness;
  if (g_state.initialized) {
    g_lcd.setBrightness(brightness);
  }
}

void displaySetRotation(uint8_t rotation) {
  g_state.rotation = rotation & 3;
  if (g_state.initialized) {
    g_lcd.setRotation(g_state.rotation);
  }
}

uint16_t displayWidth() {
  return g_state.initialized ? g_lcd.width() : ((g_state.rotation & 1) ? kLcdHeight : kLcdWidth);
}

uint16_t displayHeight() {
  return g_state.initialized ? g_lcd.height() : ((g_state.rotation & 1) ? kLcdWidth : kLcdHeight);
}

void displayFillScreen(uint16_t color) {
  if (!g_state.initialized) return;
  g_lcd.fillScreen(color);
  g_state.backgroundColor = color;
  g_lcd.setTextColor(g_state.textColor, g_state.backgroundColor);
  g_lcd.setCursor(0, 0);
}

void displayDrawPixel(int32_t x, int32_t y, uint16_t color) {
  if (g_state.initialized) g_lcd.drawPixel(x, y, color);
}

void displayFillRect(int32_t x, int32_t y, int32_t w, int32_t h, uint16_t color) {
  if (g_state.initialized) g_lcd.fillRect(x, y, w, h, color);
}

void displayDrawRect(int32_t x, int32_t y, int32_t w, int32_t h, uint16_t color) {
  if (g_state.initialized) g_lcd.drawRect(x, y, w, h, color);
}

void displayDrawLine(int32_t x0, int32_t y0, int32_t x1, int32_t y1, uint16_t color) {
  if (g_state.initialized) g_lcd.drawLine(x0, y0, x1, y1, color);
}

void displayDrawCircle(int32_t x, int32_t y, int32_t r, uint16_t color) {
  if (g_state.initialized) g_lcd.drawCircle(x, y, r, color);
}

void displayFillCircle(int32_t x, int32_t y, int32_t r, uint16_t color) {
  if (g_state.initialized) g_lcd.fillCircle(x, y, r, color);
}

void displaySetCursor(int32_t x, int32_t y) {
  if (g_state.initialized) g_lcd.setCursor(x, y);
}

void displaySetTextColor(uint16_t color) {
  g_state.textColor = color;
  if (g_state.initialized) g_lcd.setTextColor(g_state.textColor, g_state.backgroundColor);
}

void displaySetBackgroundColor(uint16_t color) {
  g_state.backgroundColor = color;
  if (g_state.initialized) g_lcd.setTextColor(g_state.textColor, g_state.backgroundColor);
}

void displaySetTextSize(uint8_t size) {
  if (size < 1 || size > 8) return;
  g_state.textSize = size;
  if (g_state.initialized) g_lcd.setTextSize(size);
}

void displayPrint(const char* text) {
  if (g_state.initialized && text != nullptr) g_lcd.print(text);
}

void displayPushRgb565(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const uint8_t* bigEndianPixels) {
  if (!g_state.initialized || bigEndianPixels == nullptr || !windowFits(x, y, w, h)) return;
  g_lcd.pushImage(x, y, w, h, reinterpret_cast<const lgfx::swap565_t*>(bigEndianPixels));
}

void displayBeginStream(uint16_t x, uint16_t y, uint16_t w, uint16_t h) {
  if (!g_state.initialized || !windowFits(x, y, w, h)) return;
  g_lcd.startWrite();
  g_lcd.setAddrWindow(x, y, w, h);
  g_state.streaming = true;
  g_state.havePendingByte = false;
}

void displayStreamBytes(const uint8_t* data, size_t len) {
  if (!g_state.streaming || data == nullptr || len == 0) return;
  // A chunk can end halfway through a pixel; carry the odd byte over.
  if (g_state.havePendingByte) {
    uint8_t pair[2] = {g_state.pendingByte, data[0]};
    g_lcd.writePixels(reinterpret_cast<const lgfx::swap565_t*>(pair), 1);
    data++;
    len--;
    g_state.havePendingByte = false;
  }
  size_t pixels = len / 2;
  if (pixels > 0) {
    g_lcd.writePixels(reinterpret_cast<const lgfx::swap565_t*>(data), static_cast<int32_t>(pixels));
  }
  if (len & 1) {
    g_state.pendingByte = data[len - 1];
    g_state.havePendingByte = true;
  }
}

void displayEndStream() {
  if (!g_state.streaming) return;
  g_lcd.endWrite();
  g_state.streaming = false;
  g_state.havePendingByte = false;
}

bool displayPlayFile(const char* name, uint16_t x, uint16_t y, uint16_t w, uint16_t h) {
  if (!g_state.initialized || name == nullptr || !windowFits(x, y, w, h) || !storageMounted()) {
    return false;
  }
  String path = storagePath(name);
  File f = LittleFS.open(path, "r");
  if (!f) {
    return false;
  }
  uint32_t expected = static_cast<uint32_t>(w) * h * 2U;
  if (f.size() < expected) {
    f.close();
    return false;
  }
  static uint8_t buf[1024];
  displayBeginStream(x, y, w, h);
  uint32_t remaining = expected;
  while (remaining > 0) {
    size_t want = remaining > sizeof(buf) ? sizeof(buf) : remaining;
    size_t got = f.read(buf, want);
    if (got == 0) {
      break;
    }
    displayStreamBytes(buf, got);
    remaining -= got;
  }
  displayEndStream();
  f.close();
  return remaining == 0;
}

void displayStatusJson(JsonObject out) {
  out["initialized"] = g_state.initialized;
  out["powered"] = g_state.powered;
  out["brightness"] = g_state.brightness;
  out["width"] = displayWidth();
  out["height"] = displayHeight();
  out["rotation"] = g_state.rotation;
  out["cursor_x"] = g_state.initialized ? g_lcd.getCursorX() : 0;
  out["cursor_y"] = g_state.initialized ? g_lcd.getCursorY() : 0;
  char hex[8];
  seleno::rgb565ToHex(g_state.textColor, hex);
  out["text_color"] = hex;
  seleno::rgb565ToHex(g_state.backgroundColor, hex);
  out["background_color"] = hex;
  out["text_size"] = g_state.textSize;
  out["spi_hz"] = kLcdSpiHz;
  out["driver"] = "LovyanGFX Panel_ST7735S";
}
