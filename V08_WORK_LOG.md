# SELENO v0.8 firmware + Pi client migration: work log

This log is for picking up the work if a session gets cut off. Read it top to
bottom before touching code. Update the **Progress** section as you go.

## Source of truth

- Hardware: `SELENO Controller v0.8 Hardware Report.txt` (repo root). It holds
  every pin, register value, the TMC2209/MCP23017 setup, the J7 Pi link,
  the GPIO18/GPIO23 reset/boot truth table and the bring-up procedure.
  The pin map is in the report section "Board pin header"
  (`seleno_v08_pins.h`).
- Old firmware (dev-kit build, being replaced): `esp32-firmware/OLDSRC/`
  (also an older copy in `esp32-firmware/temp/`). Last commit with it in
  `src/`: `1384d1b` "Fix motor issues with drifting and tracking".
- Pi client: `esp32/interfaceESP32.py`, `esp32/pins.py`, plus users in
  `core/`, `utils/` and `Client.py`.

## Branch

Work is on `firmware/v0.8`, branched from `Client/Rpi5` at `1384d1b`.
Alex committed `cfe7a1f "old setup"` (00:03) on it: old `src/` moved to
`esp32-firmware/OLDSRC/`, hardware report added (root and `esp32/`; the
`esp32/` copy has since been deleted in the working tree). All of Claude's
work is uncommitted on top of that unless Progress says otherwise.

## What the user (Alex) asked for

1. Write new ESP32 firmware for the SELENO v0.8 board (arrives about a week
   after 2026-10-15), replacing OLDSRC almost completely.
2. Redo the Pi-side serial connection: the board uses the Pi's GPIO UART
   (GPIO14/15 through J7), not a CH340 USB-serial chip.
3. **Host Pi**: development is on a Raspberry Pi 5 (this machine). The final
   host is a **Raspberry Pi Zero 2 W**. Support both.
   - Pi 5: GPIO14/15 UART0 is `/dev/ttyAMA0` with `dtparam=uart0=on`;
     Bluetooth does not share it.
   - Zero 2 W: needs `dtoverlay=disable-bt`, serial console off,
     `/dev/serial0` -> `/dev/ttyAMA0` (report, "UART link").
4. **Microstepping must be controllable at runtime over the TMC UART.**
   Also expose StealthChop vs SpreadCycle (the "silent step" mode) control.
5. **Buttons**: RETURN button press = emergency stop of all motors, handled
   locally on the ESP32. Also forward button events to the Pi, and add
   **interrupt (event) handling on the Pi side**.
6. **IMU**: add a BNO085 driver now (I2C 0x4A, on J3, separate board;
   uses clock stretching).
7. **Display**: use an established display library for the ST7735S. Do not
   hand-roll a driver.
8. ~~Do not flash the ESP32.~~ **Updated 2026-10-16: Alex OK'd flashing the
   devkit** (`pio run -e devkit -t upload`, port `/dev/ttyUSB0`). Check
   nothing else (e.g. a running `Client.py`) holds the port first.
8b. **Both USB and UART host interfaces** (Alex, 2026-10-16): the Pi side
   must work over USB (devkit / CH340, `/dev/ttyUSB*`) and over the Pi GPIO
   UART (v0.8 board via J7). Firmware must run on the devkit. Decision: the
   `devkit` env also listens on an auxiliary host UART (UART1, RX IO26 /
   TX IO25, both spare on v0.8) so the Pi's GPIO-UART path can be tested
   with jumpers (Pi GPIO14 TXD -> IO26, Pi GPIO15 RXD <- IO25, GND)
   while USB stays connected. Responses go back to the port the command came
   in on; events go to every port that has sent a command.
9. Keep this log so another AI session can continue.
10. When the hardware arrives, Alex wants Claude to test the implementation
    (Alex does the physical steps: meter checks, first power-up, observing
    LEDs/LCD/motors, pressing buttons, plugging cables with 12 V off).

## Current dev hardware

- Raspberry Pi 5, ESP32 dev kit on USB (`/dev/ttyUSB0`, CH340). No motors,
  no MCP23017, no LCD, no IMU attached. Firmware must boot and report
  "not found" for missing peripherals instead of hanging.
- The Pi client must still work against the devkit on `/dev/ttyUSB0` for now
  (port configurable), and default to the GPIO UART for the real board.

## Design decisions

- Keep the existing newline-delimited JSON protocol at 921600 baud so the
  Pi client keeps working; extend it with new commands.
- `-DCORE_DEBUG_LEVEL=0` so ESP-IDF logs don't corrupt the JSON stream.
- Pi must flush the ROM boot banner (115200 baud garbage) after resetting
  the ESP32.
- Default microsteps: **1/8** (same 1600 steps/rev as the old code, so the
  Pi's slew speeds in steps/s keep the same angular speed). Settable at runtime
  over the TMC UART. Firmware reports `steps_per_rev` / `microsteps` in
  responses; the Pi caches them per motor (never hardcodes 1600).
  Changing microsteps is refused while the motor moves; the position counter
  and speed are rescaled so degrees stay the same.
- Drivers configured over UART at boot before ENN goes low; IRUN about 1.0 A,
  hold 0.5 A (report: "Stepper drivers"). StealthChop default; SpreadCycle and
  hybrid (TPWMTHRS) selectable at runtime ("silent step" = StealthChop).
- GSTAT polled about once a second; if a driver reports `reset` (brown-out),
  firmware re-applies its config and emits a `driver_reset` event.
- DIAG (IO34/35) monitored only for drivers that answered on UART (on the
  devkit these pins float). DIAG high = driver error: that axis stops and a
  `driver_fault` event is sent.
- EXP_INT (IO39) is treated as a hint (ESP32 80 ns glitch erratum): the ISR
  only wakes the IO task, which reads GPIOA/GPIOB and compares. There's also
  a 20 ms backup poll.
- **E-stop**: RETURN press -> immediately stop all step generation, latch the
  E-stop, send `estop` event. Drivers stay enabled (holding torque, so the
  mount doesn't swing). While latched, motion commands return
  "Emergency stop active" until the Pi sends `{"cmd":"estop","action":"clear"}`.
- **Step generation**: replaced the old per-move FreeRTOS tasks with one
  hardware-timer ISR (20 kHz tick) and a 32-bit phase accumulator per axis.
  Gives exact long-term rates (sidereal), no busy-waits, both axes
  simultaneous. Max step rate 10 kHz. Command semantics unchanged
  (turn_degrees, start_continuous, set_speed speed_us/sps, stop, status,
  get_position, reset_position).
- **Motors**: the board has two fixed axes. Firmware pre-registers `motor1`
  (RA, U7, J1, TMC address 0) and `motor2` (DEC, U8, J2, address 1) at boot.
  `create_motor` on an existing id with matching (or omitted) pins
  reconfigures it (no "already exists" error); pins that match no axis are
  rejected. `delete_motor` stops, disengages and unregisters (axis can be
  re-created). `steps_per_rev` in create_motor is mapped to microsteps
  (steps_per_rev / 200).
- On the real board, `engage` fails if the driver didn't answer on UART.
  The `devkit` PlatformIO env (`-DSELENO_DEVKIT=1`) allows motion without
  drivers so the Pi stack can be exercised on the bare devkit.
- **I2C**: one bus (SDA 33 / SCL 32). No extra mutex: Wire locks each
  transaction. A "panel" task (core 0, priority 5) owns the MCP23017
  (buttons, LEDs via OLAT, E-stop); a separate lower-priority IMU task polls
  the BNO085. E-stop works even while the main loop is busy drawing.
- **IMU**: Adafruit_BNO08x library. Known risk: Adafruit says the BNO085's
  I2C clock-stretching misbehaves on ESP32. It shares the bus with the
  MCP23017 (E-stop). Mitigations: I2C timeouts, IMU in its own low-priority
  task, IMU marked lost after repeated errors. Flagged to Alex.
- **Display**: LovyanGFX (in-code panel config, ST7735S, DMA). Panel set to
  match the old working init (MADCTL 0x08: BGR, no mirroring, offsets 0,
  128x160). SPI 27 MHz (datasheet says 15 MHz; the old code ran 40 MHz fine).
  Orientation, colour order and offsets must be verified on the real LCD.
  Display init still happens on the Pi's `init` command, not at boot.
  Backlight (IO5) is driven low at boot.
- `"board"` LED channel kept as IO2 (devkit's onboard LED; unconnected on
  v0.8, harmless after boot).
- **Pi transport**: background reader thread. Lines with `"event"` go to
  event listeners (button, estop, boot, driver_*, motion_done); everything
  else goes to the response queue. Requests carry an `"id"` which the
  firmware echoes, so late responses can't be mistaken for the next one.
  Responses without an id are still accepted (old firmware).
- **Pi port selection** (`port: "auto"`): Pi 5 -> `/dev/ttyAMA0`; Zero 2 W
  -> `/dev/serial0`; then USB ports (devkit). On this Pi 5 (Ubuntu),
  `/dev/ttyAMA0` (GPIO14/15) currently has the kernel console on it
  (`console=ttyAMA0,115200` in cmdline). It must be removed before using J7
  on the Pi 5. Do not edit system config without asking Alex.
- GPIO18/23 reset/bootloader control is opt-in (only for the GPIO UART
  transport); it needs `gpiozero` + `lgpio`, which aren't installed in
  `./venv` yet.

## Plan

Firmware (`esp32-firmware/src/`), all done, built and flashed to the devkit:
- [x] board_pins.h / config.h
- [x] platformio.ini: `seleno_v08` (default), `devkit` (+aux UART, motion
      without drivers), `native` (needs host g++, not installed)
- [x] host_link (UART0 + aux UART1, ids, events, binary, **paced TX**)
- [x] stepper ISR + motors registry
- [x] tmc_drivers (config, microsteps, chopper, current, status, GSTAT, DIAG)
- [x] i2c_bus, front_panel (MCP23017 LEDs/buttons/estop), estop, imu task
- [x] display (LovyanGFX) + LittleFS playback + zlib blit
- [x] commands.cpp / commands_motor.cpp / commands_display.cpp, main.cpp
- [x] PROTOCOL.md
- [ ] native unit tests for src/logic (blocked: no g++ on this Pi)

Pi (`esp32/`, `utils/`, `core/`), all done and tested on the devkit:
- [x] interfaceESP32.py rewrite, serial_ports.py, esp32_reset.py
- [x] seleno_flash.py (J7 flashing; not run: needs board + gpiozero/lgpio)
- [x] pins.py for v0.8
- [x] event handling (utils/esp32_events.py), handlers, tracking, config
- [x] bringup.py (hardware bring-up CLI)
- [x] esp32/tests/test_interface_pty.py (12 tests, pass)

**Untested until the board arrives** (bring-up order in the report, then
`./venv/bin/python -m esp32.bringup all` and `... bringup motor`):
- TMC2209 UART config/readback, IFCNT, DRV_STATUS, DIAG, GSTAT reset path
- MCP23017 LEDs, buttons, EXP_INT interrupt, RETURN estop from the button
- LCD colours/orientation/offsets (kLcdOffset*/kLcdRgbOrder in display.cpp)
- BNO085 (known ESP32 I2C clock-stretch risk; shares bus with MCP23017)
- GPIO UART path: Pi 5 needs `console=ttyAMA0` removed from cmdline (and
  `dtparam=uart0=on`); Zero 2 W needs the report's raspi-config/disable-bt
  steps. Devkit aux UART (IO26 RX / IO25 TX) can test it with 3 jumpers.
- J7 reset/flash via GPIO18/23 (needs `pip install gpiozero lgpio`)

## Progress

- 2026-10-16: branch `firmware/v0.8` created; log started. Read OLDSRC and
  the Pi client.
- platformio.ini rewritten: envs `seleno_v08` (default), `devkit`, `native`.
  Libs installed: LovyanGFX 1.2.33, Adafruit BNO08x 1.2.7 (+BusIO, Unified
  Sensor), TMCStepper 0.7.3, ArduinoJson 7.4.3. PlatformIO is at
  `~/.platformio/penv/bin/pio` (not on PATH). Platform espressif32 7.1.3,
  arduino-esp32 2.0.17. **No host g++ on this Pi**, so `pio test -e native`
  can't run here (would need `sudo apt install g++`, not done).
- Library facts checked (don't re-check):
  - TMCStepper read() syncs on `05 FF reg`, so the single-wire echo is skipped.
    `read`/`write` are protected; `SelenoTmc` subclass exposes them.
  - Wire (2.0.17) locks per transaction, so I2C from several tasks is safe.
    `Wire.begin()` with no args is a no-op once begun on 33/32. Never call
    `Wire.end()` (a later default begin would grab IO21/22, motor pins).
  - Adafruit BNO08x prints "I2C address not found" to Serial if begin fails,
    so probe 0x4A/0x4B first. Its other debug prints are commented out.
  - LovyanGFX `rgb_order=false` + rotation 0 = MADCTL 0x08 (old init).
    `swap565_t*` = big-endian RGB565 bytes (what the Pi sends).
  - ROM miniz is at `#include <rom/miniz.h>`; `tinfl_decompressor` is ~11 KB,
    so heap-allocate it.
  - Arduino timer ISR: use `timerAttachInterruptFlag(..., ESP_INTR_FLAG_IRAM)`.
- Firmware files written so far (`esp32-firmware/src/`): board_pins.h,
  config.h, logic/{step_math,debounce,led_logic,color}.h, host_link.{h,cpp},
  stepper.{h,cpp}, tmc_drivers.{h,cpp}, motors.{h,cpp}, estop.{h,cpp},
  i2c_bus.{h,cpp}, front_panel.{h,cpp}, display.{h,cpp}, storage.{h,cpp}.
  Not yet compiled.
- host_link rewritten for multiple host ports (UART0 + aux UART1 on IO26/IO25
  when `SELENO_AUX_HOST_UART=1`, set in the devkit env). `RequestId` carries
  the port; responses go to the request's port; events go to every port
  that has sent a command (port 0 always).
- Note: a duplicate resumed session (selenoclient-30) briefly edited this log
  around 00:25 and then stopped; its Progress block was merged here.
- Next: imu.{h,cpp}, commands*.cpp, main.cpp, first build, flash devkit.

- **Handoff (this session stopping at peer selenoclient-30's request, said to
  be from Alex).** Files created/changed since the previous entry, none
  compiled yet:
  - `src/imu.h`, `src/imu.cpp`: complete, untested. BNO085 task (core 0,
    prio 2), probes 0x4A/0x4B before `begin_I2C`, calls `sh2_close()` before
    re-opening (the SH2 lib has one global SHTP instance; re-open without
    close leaks it forever). Reports: rotation vector, game rotation vector,
    gravity @ 50 Hz, calibrated mag @ 10 Hz. Events imu_found/imu_lost/imu_reset.
  - `src/commands.h`: complete. `src/commands.cpp`: complete, untested
    (dispatcher + ping, info, reboot, estop, stop_all, i2c_scan,
    panel/buttons, imu, led; falls through to display then motor handlers).
  - `src/commands_motor.cpp`: complete, untested (create_motor/delete_motor/
    list_motors + engage, disengage, enable, set_speed, turn_degrees,
    move_steps, start_continuous, stop, status, get_position,
    reset_position, set_position, set_microsteps, set_chopper, set_current,
    driver{status,reinit,read_reg,write_reg,set}).
  - `src/board_pins.h`: added PIN_AUX_HOST_RX=26 / PIN_AUX_HOST_TX=25.
  - `src/config.h`: added `SELENO_AUX_HOST_UART` (default 0).
  - `platformio.ini`: devkit env now also sets `-DSELENO_AUX_HOST_UART=1`.
  - Still NOT written: `src/commands_display.cpp` (must implement
    `handleDisplayCommand` with all old display actions + draw_pixel/text/
    rotation, and `onBinaryChunk/onBinaryComplete/onBinaryAborted` incl.
    zlib blit via `<rom/miniz.h>` with a heap `tinfl_decompressor`),
    `src/main.cpp`, `PROTOCOL.md`, and all Pi-side work.
  - Intended `setup()` order for main.cpp: `hostBegin()`; LCD BL pin (IO5)
    OUTPUT LOW; `i2cBegin()` (must precede panel/IMU); `storageBegin()`;
    `driversBegin()` (EN high) then `motorsBegin()` (stepper timer);
    `panelBegin()`; `imuBegin()`; emit `{"event":"boot",...}`. `loop()`:
    `hostPoll(); driversPoll(); motorsPoll(); hostFlushEvents();`.
    Consider `SET_LOOP_TASK_STACK_SIZE(16 * 1024)`.
  - Small known nit: `stepperStart()` doesn't clamp `stepsPerSecond` to
    `kMaxStepRateSps` (phase increment is clamped, stored rateSps isn't).
- 2026-10-16 00:31: **selenoclient-30 (this conversation, resumed) took over
  at Alex's request**; selenoclient-fb was stopped after its handoff above.
  Only one session should edit from here on.
- 00:35: wrote `src/commands_display.cpp` (all old display actions +
  draw_pixel, text/print, set_text_size, rotation; zlib blit via
  `<esp32/rom/miniz.h>`; binary callbacks) and `src/main.cpp` (setup order as
  in the handoff; 16 KB loop stack; `boot` event). All firmware sources now
  exist. Next: first build of `seleno_v08` and `devkit`, fix errors.
- 00:37: **First build OK** for `seleno_v08` and `devkit` (no warnings in
  src/ with -Wall; flash 38%, static RAM 11.5%). Not flashed yet:
  `client.service` (systemd, `/etc/systemd/system/client.service`, runs
  Client.py in screen as alex, Restart=on-failure) holds /dev/ttyUSB0.
  Alex asked to stop **and disable** it; that needs sudo (no passwordless
  sudo here). Quitting the screen session makes ExecStop fail and systemd
  restarts it, so don't try that again. Waiting for Alex to run
  `sudo systemctl disable --now client.service`. Meanwhile: Pi-side work.
- 00:45: `client.service` **stopped and disabled** (Alex set a temporary sudo
  password for this; remind Alex to change it back). To re-enable later:
  `sudo systemctl enable --now client.service`. /dev/ttyUSB0 is free.
- Pi side started: `esp32/serial_ports.py` written (port discovery for USB
  and GPIO UART; skips ports with a console= or serial-getty on them; Pi 5 ->
  /dev/ttyAMA0, others -> /dev/serial0). Remaining Pi work listed in Plan.
- Flashing devkit env next.
- 00:50: **Devkit flashed and smoke-tested over USB.** Working: ping, ids,
  events, motion (move_steps/turn_degrees/continuous + motion_done), rate
  accuracy (6.68 sps for 10.1 s -> 68 steps), estop latch/clear, microstep
  validation, create_motor pin check.
- **Finding: ESP32->Pi bytes get dropped on the dev kit's CH340 at high baud**
  (Pi 5 USB). Long replies: 53 % corrupt at 921600, 16 % at 460800, 0 % at
  115200. A bare test sketch (no tasks/ISR) is worse, so it is NOT the
  firmware: the CH340's small FIFO overflows on sustained bursts. Pi->ESP32
  is fine. **Fix: paced TX** (32-byte chunks, wait for FIFO empty, 500 us
  gap) = 200/200 clean at 921600 in the test sketch. Built into host_link
  for all ports (also guards the Zero 2 W's PL011), tunable at runtime.
  `SELENO_HOST_BAUD` build flag added to config.h (default 921600).
  Test scripts: scratchpad `smoke.py`, `corrupt.py` (not in repo).
- 00:55: Paced TX built into host_link (`PacedOut` Print wrapper per port;
  `{"cmd":"host","action":"status|set","tx_chunk","tx_gap_us"}`).
  Devkit re-flashed: **1000/1000 long replies clean at 921600**; full smoke
  test passes (microstep rescale 1000@1/8 <-> 2000@1/16 with speed scaled,
  hybrid chopper accepted, estop latch/reject/clear, motion_done reasons).
  Devkit currently runs this firmware (devkit env, 921600).
- Next: Pi side: esp32_reset.py, interfaceESP32.py rewrite (reader thread,
  ids, events), pins.py, esp32_state/config defaults, handlers, tracking.
- 01:05: Pi side written and **tested against the devkit over USB**:
  - `esp32/serial_ports.py`, `esp32/esp32_reset.py` (DTR/RTS for USB,
    GPIO18/23 via gpiozero for J7; gpiozero/lgpio NOT installed in venv).
  - `esp32/interfaceESP32.py` rewritten (old copy in scratchpad only; git has
    the original). Reader thread + event dispatcher thread, request ids,
    `ESP32Error` (error reply; message still starts "ESP32 error") vs
    `ESP32TransportError`/`TimeoutError` (link problems), read-only commands
    retried once, motion never. New: events API (`add_event_listener`,
    `wait_for_event`), `ESP32Buttons`, motor `set_microsteps/set_chopper/
    set_current/driver_status/move_steps/set_position`, `steps_per_rev` from
    firmware (cached in `conn.motor_info`), display `text/set_rotation/
    set_text_size`, conn `estop/clear_estop/stop_all/i2c_scan/panel_status/
    imu_*/host_status/set_tx_pacing/reset_esp32/reboot`.
  - `esp32/pins.py` rewritten for v0.8 (motor pins 17/16/21 and 23/22/27,
    LEDs via MCP23017, buttons, display pins, microsteps 8).
  - Test result: connect 0.23 s via auto port; all paths OK incl. blit 40 KB
    0.55 s, zlib blit, upload 0.88 s, estop events, waitUntilFinished via
    motion_done event; 0 corrupt lines.
- Next: utils/esp32_state.py + config defaults, utils/handlers.py (expose
  microsteps/current/chopper/estop), tracking.py (engage existing motors,
  steps_per_rev from firmware, reboot handling), app event handling (estop ->
  stop tracking + red LED; button hooks), bringup CLI, PROTOCOL.md, tests.
- 01:20: App wired to the new interface, **integration-tested on devkit**:
  - `utils/config_state.py`: esp32 defaults port "auto", 921600, transport.
  - `utils/esp32_state.py`: passes transport; attaches `utils/esp32_events`.
  - `utils/esp32_events.py` (new): estop -> stop Pi tracking (without the
    slow LED idle) + critical red LED; clear -> LED normal; boot ->
    `tracking.on_esp32_reboot()`; driver_* -> log (+ error LED on fault/
    lost); button -> `on_button(button, action, cb)` registry (only logging
    bound by default); link_lost -> mark disconnected. Jobs run in order on
    one worker thread (fixed a race where a slow estop LED update landed
    after the clear).
  - `core/hardware/tracking.py`: engages pre-registered motors; RA tracking
    rate uses firmware steps_per_rev; `stop_tracking(set_leds_idle=True)`;
    new `stop_tracking_for_estop()`, `on_esp32_reboot()`.
  - `utils/handlers.py`: espSetMicrosteps/espSetCurrent/espSetMode now real
    (were "not supported"); espStart -> start_continuous; espMoveSteps ->
    move_steps (was a fake degrees conversion); new espDriverStatus,
    espEstop, espClearEstop, espEstopStatus, espInfo, espPanelStatus,
    espImuRead (+ espCommand cases); ESP32Error replies no longer mark the
    ESP32 disconnected.
  - Verified: motors engaged by tracking init, handlers OK, error reply keeps
    connection, estop -> LED red in 0.3 s, clear -> normal in 1.5 s
    (LEDManager throttles 0.5 s/command), reboot -> re-init flag.
- Next: esp32/bringup.py, esp32/seleno_flash.py, PROTOCOL.md, pty tests.
- 01:30: `esp32/tests/test_interface_pty.py` (new, stdlib unittest; no
  pytest in venv): 12 tests of ESP32Connection against a fake ESP32 on a pty
  (ids, stale replies, events, old firmware, garbage, no-retry for motion,
  blit/upload framing, steps_per_rev cache, link loss). **All pass.** Run:
  `./venv/bin/python -m unittest esp32.tests.test_interface_pty -v`
- Next: esp32/bringup.py (hardware bring-up CLI), esp32/seleno_flash.py,
  esp32-firmware/PROTOCOL.md.
- 01:45: `esp32/bringup.py` (checks: info, i2c, drivers, leds, buttons,
  estop, lcd, imu, motor; dry-run on devkit fails only where hardware is
  missing), `esp32/seleno_flash.py`, `esp32-firmware/PROTOCOL.md`,
  `flash.sh` (now `./flash.sh [env=devkit] [port]`, finds pio),
  README_TMC_CONTROL.md replaced with pointers. stepperStart now clamps the
  stored rate. Final `seleno_v08` + `devkit` builds clean; devkit flashed
  with the final build; re-verified: interface test OK, 300/300 long replies
  clean, all project .py compile, all app modules import (incl. Client).
- **State at end of session:** nothing committed (branch `firmware/v0.8`).
  `client.service` is disabled and stopped (re-enable:
  `sudo systemctl enable --now client.service`). The devkit runs the devkit
  build. Alex should change the temporary sudo password back.
- 01:55: **Committed `364d00c`** "Add SELENO v0.8 firmware and GPIO-UART/USB
  Pi interface" on `firmware/v0.8` (includes Alex's deletion of the
  duplicate report in esp32/).

## Feature: automatic microsteps + chopper mode (Alex, 2026-10-16)

Goal: slew quickly and track accurately without the Pi managing driver
settings.

Design (supersedes the earlier "set_microsteps rescales the position"):
- Positions and speeds are in **logical steps** per motor: `steps_per_rev`
  (default 1600 = 200 x 8, same units the Pi already uses). The firmware
  counts position in 1/256 microsteps (int64), so any physical microstep
  setting maps exactly. Logical position = floor(pos256 * logical_m / 256).
  Reports also include `position_fine` (1/256 units).
- **Microsteps auto** (default): when a move starts (turn_degrees,
  move_steps, start_continuous) the firmware picks the finest power of two
  in [min, max] (default 4..256) whose pulse rate at the commanded speed is
  <= max_pulse_hz (default 5000). Tracking (~0.8 full steps/s) -> 1/256;
  slew 2000 sps at 1600/rev (250 full steps/s) -> 1/16. MRES change = one
  CHOPCONF write (+TPWMTHRS) while the axis is idle; position stays exact.
  `set_microsteps N` fixes it; `set_microsteps "auto"` restores auto.
- **Chopper auto** (default): TMC hybrid (TPWMTHRS) with the threshold in
  full steps/s (default 200 = 1 rev/s): StealthChop below, SpreadCycle above.
  TPWMTHRS is recomputed whenever MRES changes. Fixed modes still available.
- Finite moves at coarse microsteps can end up to < 1 physical step short of
  the logical target; the counter stays exact, so the Pi's refine pass sees
  the true position. (Slews are coarse; refine/track are fine.)
- New command `set_auto` (min_microsteps, max_microsteps, max_pulse_hz,
  spread_threshold_fsps). pins.py: microsteps "auto", chopper "auto".
- 02:10: **Auto microsteps + auto chopper implemented and tested on devkit**
  (uncommitted): stepper position now int64 1/256 microsteps + per-axis
  pulse size; motors.cpp picks microsteps per move; tmc_drivers has
  ChopperMode::Auto and `driverApplyMicrosteps` (one CHOPCONF write +
  readback); commands: set_microsteps N|"auto", set_auto, set_steps_per_rev,
  set_chopper auto/hybrid(_sps|_fsps); status has microstep_mode,
  position_fine, pulse_rate. Devkit results: sidereal 6.68 sps -> 1/256
  (214 pulses/s); 150 sps -> 1/256; 2000 sps -> 1/16 (360 deg in 0.81 s);
  9000 sps -> 1/4 (0.19 s); six moves at mixed microsteps returned exactly
  to the same 1/256 count. Pi: ESP32Motor.set_microsteps("auto"),
  set_auto, set_steps_per_rev, get_position_fine, create(chopper=);
  pins.py microsteps/chopper "auto"; handlers espSetAuto.
- Next: **acceleration ramps** (needed to slew fast without stalling an
  open-loop stepper): esp_timer 1 kHz ramp task adjusting the ISR's phase
  increment; finite moves decelerate to land on the exact pulse count;
  `stop` ramps down, estop/fault stay instant; speeds below the start speed
  (tracking, refine) start instantly. Defaults: accel 3200 sps^2 (2 rev/s^2),
  start 400 sps (0.25 rev/s), in logical units. `set_accel` command;
  espSetAccel becomes real. tracking._start_ra_tracking should wait for the
  motors to stop instead of a fixed 0.3 s sleep.
- 02:40: **Acceleration ramps done and tested on devkit.** `logic/ramp.h`
  (pure trapezoid step), 1 kHz esp_timer ramp task in stepper.cpp adjusts
  the ISR phase increment; finite moves brake to land on the exact pulse
  count; `stop` ramps from above start speed; estop/fault/disengage
  (new MotionEnd::Disengaged) are immediate. Auto microsteps now choose
  from the move's reachable peak speed (sqrt(start^2 + accel*distance)), so
  short nudges get fine steps. Defaults accel 3200 sps^2, start 400 sps.
  New `set_accel`; ESP32Motor.set_accel; handlers.espSetAccel real.
  tracking._start_ra_tracking waits for motors to stop (was fixed 0.3 s).
  bringup.py: `profile` check (tracking -> chip 1/256 + StealthChop; fast
  run -> coarse + SpreadCycle via DRV_STATUS.stealth); `motor` check
  restores auto afterwards. PROTOCOL.md motors section rewritten.
  Devkit results: 0.5/5 deg nudges at 2000 sps -> 1/64; 45 deg -> 1/32;
  360 deg @2000 -> 1/16 in 1.23 s (exact 51200); 1440 deg @6000 -> 1/8
  in 2.61 s; refine/track -> 1/256; ramped stop from 4000 sps 1.0 s; estop
  14 ms. End-to-end via tracking._move_motors + _start_ra_tracking: RA slew
  1/16, DEC refine 1/256, positions exact, tracking 1/256 @ 214 pulses/s.
  pty tests 12/12 pass. Both envs build.
- Hardware-dependent and still unverified: whether the motors (with the
  mount's load) can actually reach these speeds/accelerations in SpreadCycle
  at 12 V. Tune with `set_accel` / slew_speed_sps after `bringup profile`.
