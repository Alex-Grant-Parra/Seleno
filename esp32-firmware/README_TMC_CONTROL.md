# ESP32 firmware notes

This file used to describe an older single-driver firmware (pins 18/19,
115200 baud). That firmware is gone. For the current SELENO v0.8 firmware:

- Host protocol, including TMC2209 control (microsteps, StealthChop /
  SpreadCycle / hybrid, current, driver status): [PROTOCOL.md](PROTOCOL.md)
- Pin map: [src/board_pins.h](src/board_pins.h)
- Build envs: `seleno_v08` (the board), `devkit` (bare dev kit over USB):
  see [platformio.ini](platformio.ini)
- Hardware: `../SELENO Controller v0.8 Hardware Report.txt`
