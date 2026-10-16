// SELENO v0.8 ESP32 pin map.
// Every number comes from the KiCad netlist, as listed in
// "SELENO Controller v0.8 Hardware Report.txt", section "Board pin header".
#pragma once

#include <cstdint>

// Host link: UART0 -> J7 -> Pi GPIO14/15 (nets UART1 = TX, UART2 = RX)
constexpr int PIN_HOST_TX = 1;
constexpr int PIN_HOST_RX = 3;

// TMC2209 single-wire UART: U7 address 0, U8 address 1
constexpr int PIN_TMC_TX = 18;  // through R16 1k
constexpr int PIN_TMC_RX = 19;  // TMC_UART bus

// Motor 1 (RA): U7 -> J1
constexpr int PIN_M1_STEP = 17;
constexpr int PIN_M1_DIR  = 16;
constexpr int PIN_M1_EN   = 21;  // active low, R35 pull-up keeps it off
constexpr int PIN_M1_DIAG = 34;  // input only

// Motor 2 (DEC): U8 -> J2
constexpr int PIN_M2_STEP = 23;
constexpr int PIN_M2_DIR  = 22;
constexpr int PIN_M2_EN   = 27;  // active low, R36 pull-up keeps it off
constexpr int PIN_M2_DIAG = 35;  // input only

// I2C: MCP23017 at 0x20, IMU board on J3. Not the Arduino default 21/22,
// which are motor pins here.
constexpr int PIN_SDA     = 33;
constexpr int PIN_SCL     = 32;
constexpr int PIN_EXP_INT = 39;  // MCP23017 INTA, active low, R38 pull-up

// LCD: ST7735S on S1, CS tied low on the connector
constexpr int PIN_LCD_SCK  = 14;
constexpr int PIN_LCD_MOSI = 13;
constexpr int PIN_LCD_DC   = 15;
constexpr int PIN_LCD_RST  = 4;
constexpr int PIN_LCD_BL   = 5;
constexpr int PIN_LCD_CS   = -1;

// Unconnected on v0.8 (strap, low at boot). It is the onboard LED on the
// dev kit, so it backs the "board" LED channel. Driving it after boot is safe.
constexpr int PIN_BOARD_LED = 2;

// Auxiliary host UART (devkit builds only, SELENO_AUX_HOST_UART=1). Both pins
// are spare on v0.8. Wire Pi GPIO14 (TXD) -> IO26 and Pi GPIO15 (RXD) <- IO25.
constexpr int PIN_AUX_HOST_RX = 26;
constexpr int PIN_AUX_HOST_TX = 25;

constexpr uint8_t MCP23017_ADDR = 0x20;
constexpr uint8_t BNO08X_ADDR   = 0x4A;  // 0x4B if the IMU board's SA0 is high
constexpr uint8_t BNO08X_ADDR_ALT = 0x4B;
constexpr float   TMC_RSENSE    = 0.15f;  // ohms (R25-R28)

// MCP23017 port bits (report, "Buttons and LEDs: U1").
// Port A
constexpr uint8_t MCP_A_YELLOW = 0;
constexpr uint8_t MCP_A_BLUE   = 1;
constexpr uint8_t MCP_A_SELECT = 2;
// Port B
constexpr uint8_t MCP_B_RETURN = 0;
constexpr uint8_t MCP_B_TOP    = 1;
constexpr uint8_t MCP_B_BOTTOM = 2;
constexpr uint8_t MCP_B_LEFT   = 3;
constexpr uint8_t MCP_B_RIGHT  = 4;
constexpr uint8_t MCP_B_RED    = 5;
constexpr uint8_t MCP_B_GREEN  = 6;
constexpr uint8_t MCP_B_WHITE  = 7;
