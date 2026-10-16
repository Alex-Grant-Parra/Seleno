// ST7735S 128x160 LCD on S1, driven by LovyanGFX.
//
// Panel set-up matches the old hand-written driver that worked on Alex's
// module: rotation 0 gives MADCTL 0x08 (BGR, no mirroring), offsets 0.
// Check colours, orientation and edges on the real board (bring-up step 10);
// adjust kLcdOffset*/kLcdRgbOrder in display.cpp if needed.
//
// Pixel payloads from the Pi are RGB565 big-endian (high byte first), which
// is the panel's own byte order.
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include <cstdint>

bool displayInit();
bool displayInitialized();
void displayPower(bool on);
void displayBacklight(uint8_t brightness);
void displaySetRotation(uint8_t rotation);
uint16_t displayWidth();
uint16_t displayHeight();

void displayFillScreen(uint16_t color);
void displayDrawPixel(int32_t x, int32_t y, uint16_t color);
void displayFillRect(int32_t x, int32_t y, int32_t w, int32_t h, uint16_t color);
void displayDrawRect(int32_t x, int32_t y, int32_t w, int32_t h, uint16_t color);
void displayDrawLine(int32_t x0, int32_t y0, int32_t x1, int32_t y1, uint16_t color);
void displayDrawCircle(int32_t x, int32_t y, int32_t r, uint16_t color);
void displayFillCircle(int32_t x, int32_t y, int32_t r, uint16_t color);

void displaySetCursor(int32_t x, int32_t y);
void displaySetTextColor(uint16_t color);
void displaySetBackgroundColor(uint16_t color);
void displaySetTextSize(uint8_t size);
void displayPrint(const char* text);

// Whole-window pixel writes.
void displayPushRgb565(uint16_t x, uint16_t y, uint16_t w, uint16_t h, const uint8_t* bigEndianPixels);
// Chunked writes for frames that don't fit in RAM. Chunks may split a pixel.
void displayBeginStream(uint16_t x, uint16_t y, uint16_t w, uint16_t h);
void displayStreamBytes(const uint8_t* data, size_t len);
void displayEndStream();

// Show a raw RGB565 file (w*h*2 bytes) stored on LittleFS.
bool displayPlayFile(const char* name, uint16_t x, uint16_t y, uint16_t w, uint16_t h);

void displayStatusJson(JsonObject out);
