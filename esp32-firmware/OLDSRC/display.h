#ifndef DISPLAY_H
#define DISPLAY_H

#include <Arduino.h>
#include <cstdint>

// ST7735S TFT LCD Pin Configuration
#define DISPLAY_SCK   13   // D13 - SPI Clock
#define DISPLAY_SDA   19   // D19 - SPI MOSI (Data)
#define DISPLAY_DC    21   // D21 - Data/Command
#define DISPLAY_RES   12   // D12 - Reset
#define DISPLAY_CS    -1   // GND (tied to ground)
#define DISPLAY_BL    15   // D15 - PWM Backlight

// Display dimensions
#define DISPLAY_WIDTH   128
#define DISPLAY_HEIGHT  160
#define DISPLAY_BPP     16  // 16-bit color

// Color definitions (RGB565)
#define COLOR_BLACK     0x0000
#define COLOR_RED       0xF800
#define COLOR_GREEN     0x07E0
#define COLOR_BLUE      0x001F
#define COLOR_WHITE     0xFFFF
#define COLOR_YELLOW    0xFFE0
#define COLOR_CYAN      0x07FF
#define COLOR_MAGENTA   0xF81F

struct DisplayState {
  bool initialized;
  bool powered;
  uint8_t brightness;        // 0-255 PWM value for backlight
  uint16_t background_color;
  uint16_t text_color;
  uint8_t text_size;
  uint16_t cursor_x;
  uint16_t cursor_y;
};

extern DisplayState g_display_state;

// Initialization and control
void initializeDisplay();
void cleanupDisplay();
void displayPower(bool on);
void displayBacklight(uint8_t brightness);

// Drawing functions
void displayFillScreen(uint16_t color);
void displayDrawPixel(uint16_t x, uint16_t y, uint16_t color);
void displayDrawRectangle(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t color);
void displayFillRectangle(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t color);
void displayDrawCircle(uint16_t x, uint16_t y, uint16_t r, uint16_t color);
void displayFillCircle(uint16_t x, uint16_t y, uint16_t r, uint16_t color);
void displayDrawLine(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1, uint16_t color);
void displayBeginBlit(uint16_t x, uint16_t y, uint16_t w, uint16_t h);
void displayWriteBlitData(const uint8_t* data, size_t len);
void displayEndBlit();

// Text functions
void displaySetTextColor(uint16_t color);
void displaySetBackgroundColor(uint16_t color);
void displaySetTextSize(uint8_t size);
void displaySetCursor(uint16_t x, uint16_t y);
void displayPrintText(const String& text);
void displayPrintLine(const String& text);
void displayClearLine(uint16_t line_num);

// Utility functions
uint16_t hexToColor(const String& hex_str);
String colorToHex(uint16_t color);
void displayGetState();
// Play a stored file from LittleFS. For now expects a raw RGB565 file sized
// exactly w*h*2 bytes; the file will be streamed to the display at the
// specified x,y position.
void displayPlayFile(const char* name, uint16_t x, uint16_t y, uint16_t w, uint16_t h);

#endif // DISPLAY_H
