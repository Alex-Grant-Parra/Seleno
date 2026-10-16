// Firmware-wide tunables. Pins live in board_pins.h.
#pragma once

#include <cstdint>

#define SELENO_FW_VERSION "0.8.0"
#define SELENO_BOARD_NAME "seleno-v0.8"

#ifndef SELENO_DEVKIT
#define SELENO_DEVKIT 0
#endif

// Second host port on UART1 (see host_link.h). On in the devkit env.
#ifndef SELENO_AUX_HOST_UART
#define SELENO_AUX_HOST_UART 0
#endif

// Host link (UART0 to the Pi). The PL011 on the Pi reaches 921600 with
// about 0.2 % error.
#ifndef SELENO_HOST_BAUD
#define SELENO_HOST_BAUD 921600
#endif
constexpr uint32_t kHostBaud = SELENO_HOST_BAUD;
// Replies to the host go out in short bursts with a gap after each one. A
// dev kit's CH340 drops ESP32->Pi bytes on sustained 921600 bursts (53 % of
// 300-byte replies corrupted, 0 % when paced); the Zero 2 W's PL011 gets
// the same protection. About 37 KB/s, ample for replies and events.
// Tunable at runtime with {"cmd":"host","action":"set",...}; chunk 0 = off.
constexpr uint16_t kHostTxChunkBytes = 32;
constexpr uint16_t kHostTxGapUs = 500;
// Large enough to absorb bursts while the main loop is busy drawing.
constexpr size_t kHostRxBufferBytes = 8192;
constexpr size_t kMaxCommandLineBytes = 4096;
// Abort a binary transfer if the host goes quiet for this long.
constexpr uint32_t kTransferTimeoutMs = 5000;

// TMC2209 single-wire UART. 115200 is the report's "safe choice".
constexpr uint32_t kTmcBaud = 115200;
constexpr uint8_t kTmcAddrMotor1 = 0;  // U7: MS1 = MS2 = GND
constexpr uint8_t kTmcAddrMotor2 = 1;  // U8: MS1 = 3.3 V
// Report: "Recommended run current for the 1.5 A NEMA17s" is about 1.0 A
// (IRUN 23); hold about half of that.
constexpr uint16_t kDefaultRunCurrentMa = 1000;
constexpr float kDefaultHoldMultiplier = 0.5f;
// Report: 1.35 A RMS is the hardware maximum with 0.15 ohm sense resistors.
constexpr uint16_t kMaxRunCurrentMa = 1350;
// 1/8 keeps the old 1600 steps/rev, so the Pi's speeds in steps/s are unchanged.
constexpr uint16_t kDefaultMicrosteps = 8;
constexpr uint16_t kFullStepsPerRev = 200;  // 1.8 degree motors
constexpr uint32_t kDriverPollIntervalMs = 1000;

// Step generation: one hardware-timer tick drives both axes.
constexpr uint32_t kStepTickHz = 20000;
// A STEP pulse is high for one tick and low for at least one, so the rate
// can't exceed half the tick rate.
constexpr float kMaxStepRateSps = kStepTickHz / 2.0f - 1.0f;
constexpr uint32_t kDefaultStepDelayUs = 1250;  // same default as the old code

// I2C bus (MCP23017 + IMU board).
constexpr uint32_t kI2cHz = 400000;
constexpr uint16_t kI2cTimeoutMs = 50;
// MCP23017 RESET rises through R19 10k / C33 10 uF (tau = 100 ms); the report
// says wait at least 200 ms after power-up before the first access.
constexpr uint32_t kExpanderPowerUpMs = 250;

// Buttons
constexpr uint32_t kButtonDebounceMs = 15;
constexpr uint32_t kButtonBackupPollMs = 20;
constexpr uint32_t kIoTaskTickMs = 5;

// LCD. The ST7735S datasheet allows 15 MHz writes; the old firmware ran this
// module at 40 MHz without trouble. 27 MHz is a middle ground.
constexpr uint32_t kLcdSpiHz = 27000000;
constexpr uint16_t kLcdWidth = 128;
constexpr uint16_t kLcdHeight = 160;
constexpr uint8_t kLcdBacklightPwmChannel = 7;

// IMU (BNO085)
constexpr uint32_t kImuReportIntervalUs = 20000;  // 50 Hz
constexpr uint32_t kImuPollIntervalMs = 10;
constexpr uint8_t kImuMaxConsecutiveErrors = 20;
