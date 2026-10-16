#include "display.h" 
#include <SPI.h>
#include <LittleFS.h>

// Global display state
DisplayState g_display_state = {
  .initialized = false,
  .powered = true,
  .brightness = 255,
  .background_color = COLOR_BLACK,
  .text_color = COLOR_WHITE,
  .text_size = 1,
  .cursor_x = 0,
  .cursor_y = 0
};

// Forward declarations for low-level SPI operations
static void displayWrite8(uint8_t byte);
static void displayWrite16(uint16_t word);
static void displayWriteBytes(const uint8_t* data, size_t len);
static void displayCommandMode();
static void displayDataMode();
static void displayReset();
static void displaySetWindow(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1);
static void displayFillCircleHelper(uint16_t x, uint16_t y, uint16_t r, uint8_t corners, uint16_t delta, uint16_t color);
static String normalizeFsPath(const char* name) {
  String path(name == nullptr ? "" : name);
  path.trim();
  if (path.length() > 0 && path[0] != '/') {
    path = "/" + path;
  }
  return path;
}

// SPI communication helpers
static void displayWrite8(uint8_t byte) {
  SPI.write(byte);
}

static void displayWrite16(uint16_t word) {
  displayWrite8(word >> 8);
  displayWrite8(word & 0xFF);
}

static void displayWriteBytes(const uint8_t* data, size_t len) {
  if (data == nullptr || len == 0) {
    return;
  }

  SPI.writeBytes(data, len);
  if ((len & 0x3FF) == 0) {
    yield();
  }
}

static void displayCommandMode() {
  digitalWrite(DISPLAY_DC, LOW);
}

static void displayDataMode() {
  digitalWrite(DISPLAY_DC, HIGH);
}

static void displayReset() {
  digitalWrite(DISPLAY_RES, LOW);
  delay(10);
  digitalWrite(DISPLAY_RES, HIGH);
  delay(10);
}

static void displaySetWindow(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1) {
  // Column address set (CASET)
  displayCommandMode();
  displayWrite8(0x2A);
  displayDataMode();
  displayWrite16(x0);
  displayWrite16(x1);
  
  // Row address set (RASET)
  displayCommandMode();
  displayWrite8(0x2B);
  displayDataMode();
  displayWrite16(y0);
  displayWrite16(y1);
}

// Initialize display
void initializeDisplay() {
  if (g_display_state.initialized) {
    return;
  }

  // Initialize pins
  pinMode(DISPLAY_DC, OUTPUT);
  pinMode(DISPLAY_RES, OUTPUT);
  pinMode(DISPLAY_BL, OUTPUT);
  // Keep backlight off until the panel RAM is explicitly cleared.
  analogWrite(DISPLAY_BL, 0);

  // Initialize SPI with explicit parameters
  // SPI.begin(SCK, MISO, MOSI, CS)
  // We don't use MISO (read-only) or CS (tied to ground)
  SPI.begin(DISPLAY_SCK, -1, DISPLAY_SDA, -1);
  SPI.setFrequency(40000000);  // 40 MHz for display throughput
  SPI.setDataMode(SPI_MODE0);

  // Reset display
  displayReset();

  // Initialize ST7735S - using smaller delays to avoid blocking serial
  // Sleep out
  displayCommandMode();
  displayWrite8(0x11);  // SLPOUT
  delay(10);
  
  // Yield to allow serial handler to run
  yield();

  // Interface Pixel Format - 16-bit/pixel (RGB565)
  displayCommandMode();
  displayWrite8(0x3A);  // COLMOD
  displayDataMode();
  displayWrite8(0x05);  // 16-bit/pixel
  
  delay(5);
  yield();

  // Memory Data Access Control - BGR mode
  displayCommandMode();
  displayWrite8(0x36);  // MADCTL
  displayDataMode();
  displayWrite8(0x08);  // BGR
  
  delay(5);
  yield();
  // Display on
  displayCommandMode();
  displayWrite8(0x29);  // DISPON
  
  delay(10);
  yield();

  // Set column address range (CASET)
  displayCommandMode();
  displayWrite8(0x2A);
  displayDataMode();
  displayWrite8(0x00);
  displayWrite8(0x00);
  displayWrite8(0x00);
  displayWrite8(0x7F);  // 128-1
  
  delay(2);
  yield();
  
  // Set row address range (RASET)
  displayCommandMode();
  displayWrite8(0x2B);
  displayDataMode();
  displayWrite8(0x00);
  displayWrite8(0x00);
  displayWrite8(0x00);
  displayWrite8(0x9F);  // 160-1
  
  delay(2);
  yield();

  g_display_state.initialized = true;
  // Ensure deterministic startup visuals (no random VRAM/static).
  displayFillScreen(COLOR_BLACK);

  // Set backlight to full brightness after clear completes.
  analogWrite(DISPLAY_BL, 255);
}

void cleanupDisplay() {
  if (!g_display_state.initialized) {
    return;
  }
  displayPower(false);
  SPI.end();
  g_display_state.initialized = false;
}

void displayPower(bool on) {
  if (!g_display_state.initialized) {
    return;
  }
  
  displayCommandMode();
  if (on) {
    displayWrite8(0x29);  // DISPON
    g_display_state.powered = true;
  } else {
    displayWrite8(0x28);  // DISPOFF
    g_display_state.powered = false;
  }
}

void displayBacklight(uint8_t brightness) {
  if (!g_display_state.initialized) {
    return;
  }
  g_display_state.brightness = brightness;
  analogWrite(DISPLAY_BL, brightness);
}

// Fill screen with color
void displayFillScreen(uint16_t color) {
  if (!g_display_state.initialized) {
    return;
  }
  
  displaySetWindow(0, 0, DISPLAY_WIDTH - 1, DISPLAY_HEIGHT - 1);
  
  displayCommandMode();
  displayWrite8(0x2C);  // RAMWR - Write to RAM
  
  displayDataMode();
  for (uint16_t i = 0; i < DISPLAY_WIDTH * DISPLAY_HEIGHT; i++) {
    displayWrite16(color);
    // Yield every 256 pixels to prevent blocking serial handler
    if ((i & 0xFF) == 0) {
      yield();
    }
  }
  
  g_display_state.background_color = color;
  g_display_state.cursor_x = 0;
  g_display_state.cursor_y = 0;
}

// Signed-coordinate primitives. Shapes near the top/left edge produce negative
// coordinates, which must be clipped rather than wrapped to ~65535.
static void drawPixelClipped(int32_t x, int32_t y, uint16_t color) {
  if (x < 0 || y < 0 || x >= DISPLAY_WIDTH || y >= DISPLAY_HEIGHT) {
    return;
  }

  displaySetWindow(x, y, x, y);

  displayCommandMode();
  displayWrite8(0x2C);  // RAMWR

  displayDataMode();
  displayWrite16(color);
}

static void fillRectClipped(int32_t x, int32_t y, int32_t w, int32_t h, uint16_t color) {
  if (x < 0) { w += x; x = 0; }
  if (y < 0) { h += y; y = 0; }
  if (x + w > DISPLAY_WIDTH) { w = DISPLAY_WIDTH - x; }
  if (y + h > DISPLAY_HEIGHT) { h = DISPLAY_HEIGHT - y; }
  if (w <= 0 || h <= 0) {
    return;
  }

  displaySetWindow(x, y, x + w - 1, y + h - 1);

  displayCommandMode();
  displayWrite8(0x2C);  // RAMWR

  displayDataMode();
  uint32_t pixel_count = (uint32_t)w * h;
  for (uint32_t i = 0; i < pixel_count; i++) {
    displayWrite16(color);
    // Yield every 256 pixels to prevent blocking serial handler
    if ((i & 0xFF) == 0) {
      yield();
    }
  }
}

// Bresenham's algorithm
static void drawLineClipped(int32_t x0, int32_t y0, int32_t x1, int32_t y1, uint16_t color) {
  int32_t dx = (x1 > x0) ? (x1 - x0) : (x0 - x1);
  int32_t dy = (y1 > y0) ? (y1 - y0) : (y0 - y1);
  int32_t sx = (x0 < x1) ? 1 : -1;
  int32_t sy = (y0 < y1) ? 1 : -1;
  int32_t err = dx - dy;

  while (1) {
    drawPixelClipped(x0, y0, color);
    if (x0 == x1 && y0 == y1) break;

    int32_t e2 = 2 * err;
    if (e2 > -dy) {
      err -= dy;
      x0 += sx;
    }
    if (e2 < dx) {
      err += dx;
      y0 += sy;
    }
  }
}

// Draw single pixel
void displayDrawPixel(uint16_t x, uint16_t y, uint16_t color) {
  if (!g_display_state.initialized) {
    return;
  }
  drawPixelClipped(x, y, color);
}

// Draw rectangle (outline)
void displayDrawRectangle(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t color) {
  if (!g_display_state.initialized || w == 0 || h == 0) {
    return;
  }

  fillRectClipped(x, y, w, 1, color);              // Top
  fillRectClipped(x, (int32_t)y + h - 1, w, 1, color);  // Bottom
  fillRectClipped(x, y, 1, h, color);              // Left
  fillRectClipped((int32_t)x + w - 1, y, 1, h, color);  // Right
}

// Fill rectangle
void displayFillRectangle(uint16_t x, uint16_t y, uint16_t w, uint16_t h, uint16_t color) {
  if (!g_display_state.initialized) {
    return;
  }
  fillRectClipped(x, y, w, h, color);
}

// Draw circle (outline)
void displayDrawCircle(uint16_t x, uint16_t y, uint16_t r, uint16_t color) {
  if (!g_display_state.initialized) {
    return;
  }

  int32_t cx = x;
  int32_t cy = y;
  int32_t f = 1 - r;
  int32_t ddF_x = 1;
  int32_t ddF_y = -2 * (int32_t)r;
  int32_t px = 0;
  int32_t py = r;

  drawPixelClipped(cx, cy + r, color);
  drawPixelClipped(cx, cy - r, color);
  drawPixelClipped(cx + r, cy, color);
  drawPixelClipped(cx - r, cy, color);

  while (px < py) {
    if (f >= 0) {
      py--;
      ddF_y += 2;
      f += ddF_y;
    }
    px++;
    ddF_x += 2;
    f += ddF_x;

    drawPixelClipped(cx + px, cy + py, color);
    drawPixelClipped(cx - px, cy + py, color);
    drawPixelClipped(cx + px, cy - py, color);
    drawPixelClipped(cx - px, cy - py, color);
    drawPixelClipped(cx + py, cy + px, color);
    drawPixelClipped(cx - py, cy + px, color);
    drawPixelClipped(cx + py, cy - px, color);
    drawPixelClipped(cx - py, cy - px, color);
  }
}

// Fill circle
void displayFillCircle(uint16_t x, uint16_t y, uint16_t r, uint16_t color) {
  if (!g_display_state.initialized) {
    return;
  }

  fillRectClipped(x, (int32_t)y - r, 1, 2 * (int32_t)r + 1, color);
  displayFillCircleHelper(x, y, r, 3, 0, color);
}

// Helper for filled circle: draws vertical spans either side of the centre
static void displayFillCircleHelper(uint16_t x, uint16_t y, uint16_t r, uint8_t corners, uint16_t delta, uint16_t color) {
  int32_t cx = x;
  int32_t cy = y;
  int32_t f = 1 - r;
  int32_t ddF_x = 1;
  int32_t ddF_y = -2 * (int32_t)r;
  int32_t px = 0;
  int32_t py = r;

  while (px < py) {
    if (f >= 0) {
      py--;
      ddF_y += 2;
      f += ddF_y;
    }
    px++;
    ddF_x += 2;
    f += ddF_x;

    if (corners & 1) {
      fillRectClipped(cx + px, cy - py, 1, 2 * py + 1 + delta, color);
      fillRectClipped(cx + py, cy - px, 1, 2 * px + 1 + delta, color);
    }
    if (corners & 2) {
      fillRectClipped(cx - px, cy - py, 1, 2 * py + 1 + delta, color);
      fillRectClipped(cx - py, cy - px, 1, 2 * px + 1 + delta, color);
    }
  }
}

// Draw line
void displayDrawLine(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1, uint16_t color) {
  if (!g_display_state.initialized) {
    return;
  }
  drawLineClipped(x0, y0, x1, y1, color);
}

void displayBeginBlit(uint16_t x, uint16_t y, uint16_t w, uint16_t h) {
  if (!g_display_state.initialized) {
    return;
  }

  if (w == 0 || h == 0) {
    return;
  }

  displaySetWindow(x, y, x + w - 1, y + h - 1);
  displayCommandMode();
  displayWrite8(0x2C);  // RAMWR
  displayDataMode();
}

void displayWriteBlitData(const uint8_t* data, size_t len) {
  if (!g_display_state.initialized || data == nullptr || len == 0) {
    return;
  }

  displayWriteBytes(data, len);
}

void displayEndBlit() {
  // No-op; kept for symmetry and future hooks.
}

void displayPlayFile(const char* name, uint16_t x, uint16_t y, uint16_t w, uint16_t h) {
  if (!g_display_state.initialized || name == nullptr) {
    return;
  }

  // The frame is streamed straight into panel RAM, so it cannot be clipped;
  // reject windows that fall outside the screen.
  if (w == 0 || h == 0 || (uint32_t)x + w > DISPLAY_WIDTH || (uint32_t)y + h > DISPLAY_HEIGHT) {
    return;
  }

  if (!LittleFS.begin(true)) {
    return;
  }

  String fname = normalizeFsPath(name);
  if (!LittleFS.exists(fname)) {
    return;
  }

  File f = LittleFS.open(fname, "r");
  if (!f) {
    return;
  }

  uint32_t fileSize = f.size();
  uint32_t expected = (uint32_t)w * (uint32_t)h * 2U;
  if (fileSize < expected) {
    // Not enough data for a single frame
    f.close();
    return;
  }

  // Set window and begin RAM write
  displaySetWindow(x, y, x + w - 1, y + h - 1);
  displayCommandMode();
  displayWrite8(0x2C); // RAMWR
  displayDataMode();

  const size_t bufSize = 512;
  uint8_t buf[bufSize];
  uint32_t remaining = expected;
  while (remaining > 0) {
    size_t toRead = (remaining > bufSize) ? bufSize : remaining;
    size_t r = f.read(buf, toRead);
    if (r == 0) break;
    displayWriteBytes(buf, r);
    remaining -= r;
    if ((remaining & 0x3FF) == 0) yield();
  }

  f.close();
}

// Text color functions
void displaySetTextColor(uint16_t color) {
  g_display_state.text_color = color;
}

void displaySetBackgroundColor(uint16_t color) {
  g_display_state.background_color = color;
}

void displaySetTextSize(uint8_t size) {
  if (size > 0 && size <= 8) {
    g_display_state.text_size = size;
  }
}

void displaySetCursor(uint16_t x, uint16_t y) {
  if (x < DISPLAY_WIDTH && y < DISPLAY_HEIGHT) {
    g_display_state.cursor_x = x;
    g_display_state.cursor_y = y;
  }
}

// Print text at current cursor position
void displayPrintText(const String& text) {
  if (!g_display_state.initialized) {
    return;
  }
  
  const uint8_t char_width = 6 * g_display_state.text_size;
  const uint8_t char_height = 8 * g_display_state.text_size;
  
  for (size_t i = 0; i < text.length(); i++) {
    uint16_t next_x = g_display_state.cursor_x + char_width;
    if (next_x > DISPLAY_WIDTH) {
      g_display_state.cursor_x = 0;
      g_display_state.cursor_y += char_height;
    }
    if (g_display_state.cursor_y + char_height > DISPLAY_HEIGHT) {
      g_display_state.cursor_y = 0;
    }
    
    // Simple character drawing placeholder
    // In a real implementation, you'd draw individual pixels for each character
    displaySetWindow(g_display_state.cursor_x, g_display_state.cursor_y,
                     g_display_state.cursor_x + char_width - 1,
                     g_display_state.cursor_y + char_height - 1);
    displayCommandMode();
    displayWrite8(0x2C);  // RAMWR
    displayDataMode();
    for (uint16_t j = 0; j < char_width * char_height; j++) {
      displayWrite16(g_display_state.text_color);
    }
    
    g_display_state.cursor_x = next_x;
    yield();  // Yield after each character
  }
}

void displayPrintLine(const String& text) {
  displayPrintText(text);
  g_display_state.cursor_x = 0;
  g_display_state.cursor_y += 8 * g_display_state.text_size;
}

void displayClearLine(uint16_t line_num) {
  if (!g_display_state.initialized) {
    return;
  }
  
  uint16_t y = line_num * (8 * g_display_state.text_size);
  uint16_t height = 8 * g_display_state.text_size;
  
  if (y < DISPLAY_HEIGHT) {
    displayFillRectangle(0, y, DISPLAY_WIDTH, height, g_display_state.background_color);
  }
}

// Utility: Convert hex string to RGB565 color
uint16_t hexToColor(const String& hex_str) {
  unsigned long hex = strtoul(hex_str.c_str(), nullptr, 16);
  
  uint8_t r = (hex >> 16) & 0xFF;
  uint8_t g = (hex >> 8) & 0xFF;
  uint8_t b = hex & 0xFF;
  
  // Convert to RGB565
  uint16_t r5 = (r >> 3) & 0x1F;
  uint16_t g6 = (g >> 2) & 0x3F;
  uint16_t b5 = (b >> 3) & 0x1F;
  
  return (r5 << 11) | (g6 << 5) | b5;
}

String colorToHex(uint16_t color) {
  uint8_t r5 = (color >> 11) & 0x1F;
  uint8_t g6 = (color >> 5) & 0x3F;
  uint8_t b5 = color & 0x1F;
  
  uint8_t r = (r5 << 3) | (r5 >> 2);
  uint8_t g = (g6 << 2) | (g6 >> 4);
  uint8_t b = (b5 << 3) | (b5 >> 2);
  
  char hex[7];
  snprintf(hex, sizeof(hex), "%02X%02X%02X", r, g, b);
  return String(hex);
}

void displayGetState() {
  // This is called by displayGetStatus in commands.cpp
}
