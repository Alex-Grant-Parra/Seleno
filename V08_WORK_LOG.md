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
- 03:00: Committed `9750334` "Add automatic microsteps, chopper mode and
  acceleration ramps". Answered Alex's BNO085-on-I2C questions (it should
  work; 100 kHz + SDA-only pull-up are the mitigations; nothing changed yet).

## Feature: front-panel menu UI (Alex, 2026-10-16)

Alex's request, verbatim points:
- First power on: while the Pi boots and until there is a stable Pi<->ESP32
  connection, show a loading screen ("SELENO" is enough for now).
- Once connected: a menu. On first run it must let you set the **domain,
  API key and device name**; on later runs they can be changed in a config
  panel. Pi-side these are `client_config.base_url`, `api_token`,
  `client_id` in `config/client_profile.json` (Client.py used to exit 78
  when missing).
- Small **on-screen keyboard** driven by the pad: 4 directions, enter
  (SELECT), return.
- Must be able to **test all functions of the board** from the menu.
- **Easy to add panels/options.** Panels are horizontal tabs at the top;
  selecting one shows its options.
- **RETURN pressed twice quickly while tracking: back to the menu and stop
  tracking, without needing the Pi.**
- Add other useful features.

Design:
- The UI runs **on the ESP32** in the main loop (`uiPoll()` from loop()), so
  the display, TMC UART and motor calls stay single-threaded. It renders into
  a full-frame 16-bit LovyanGFX sprite (40 KB; heap free was 275 KB) and
  pushes it. Files: `src/ui*.cpp/h` (flat, like commands_*.cpp).
- Two sources of panels, both shown as tabs sorted by `order`:
  1. **Local (C++)** panels built in `ui_panels.cpp` with a small builder
     (action/toggle/choice/number/text/info/screen items): Motors, Test,
     Display, About.
  2. **Pi panels** sent as JSON with `{"cmd":"ui","action":"menu",...}` and
     defined declaratively in Python (`utils/panel_menu.py`): Scope, Setup,
     Pi/System. Changes/actions come back as `{"event":"ui",...}`.
- Link: the Pi sends `ui hello` when it opens the port (and after an ESP32
  `boot` event), then `ui status` every 2 s. Loading screen until hello.
  No host bytes for 6 s => "Pi offline" (status bar), Pi panels greyed.
  Hold SELECT on the loading screen => offline menu (board tests without Pi).
- First run: Pi-driven wizard using `ui prompt` (text/confirm/message);
  Client.py waits for a complete config instead of exiting 78.
- **RETURN semantics** (panel task, independent of the main loop):
  * Host motion faster than 50 full steps/s (a slew): single RETURN press =
    latched E-stop, as before.
  * Host motion slower (tracking/refine) and the UI is not in a navigation
    screen: RETURN twice within 500 ms = "panel stop": stop all motors,
    set a **motion hold** that refuses *host* motion commands until the Pi
    acks (`ui ack_stop`), emit `{"event":"ui","type":"stop"}`, UI -> menu.
    Works with the Pi offline; the hello reply reports a pending stop.
  * Otherwise RETURN is the menu's back key.
  Motion is tagged with its source (Host vs Panel); UI tests are Panel.
- Host display commands that draw put the UI in "host display" mode (UI
  stops drawing) until RETURN or `ui mode`. UI rotation is separate from
  the host's (assets are 128x160 portrait).
- Extras: night (red) theme, brightness, auto-dim, LED mute, screenshots
  over serial (`ui screenshot`, used to check the UI on the screenless
  devkit), key injection (`ui key`), tracking screen, self-test, Pi
  reboot/shutdown/restart-client actions, IP/Wi-Fi/server status.

### Handoff: menu UI status (2026-10-16, session ended at Alex's request)

**State:** firmware side written; **builds clean** for `devkit` and `seleno_v08`
(no warnings; RAM 12 %, flash 50 %). **Not flashed and not run yet.** Alex
rejected the flash at the end of the session because his usage limit was
nearly used up, not because of a problem. Flashing the devkit is still
allowed. **Nothing is committed** (branch `firmware/v0.8`, last commit
`9750334`). The Pi side has **not been started**.

New firmware files (`esp32-firmware/src/`):
- `ui.h`: outside API (`uiBegin`, `uiPoll`, `uiHandleCommand`,
  `uiHostDrawing`, `uiLedTestActive`).
- `ui_internal.h`: Screen base class (key/tick/draw/refreshMs/
  returnNavigates/footerHint/stateJson), Key/KeyEvent, Theme, fonts,
  Settings (NVS "ui"), link state, drawing helpers.
- `ui_core.cpp`: screen stack, key queue from the panel task plus injected
  keys, repeat/long press, double RETURN flag, auto-dim, Pi link state
  (hello / 6 s timeout), status values and clock, host-display mode, render
  loop into a 16-bit 128x160 sprite, panel-stop notice -> menu.
- `ui_menu.h/.cpp`: MenuItem/MenuPanel model, `PanelBuilder` (`addPanel`),
  Pi panels from JSON (`setRemotePanels`, `setRemoteValues`,
  `updateRemoteItem`), and MenuScreen. Tabs focus first; DOWN/SELECT enter
  the list; UP from the top item or RETURN goes back to the tabs. LEFT/RIGHT
  adjust toggle/choice/number items. SELECT: run / flip / choice list /
  number edit mode / keyboard / open screen; on an info item it shows the
  full text. RETURN on the tabs pops back to tracking if the menu was opened
  over it.
- `ui_keyboard.cpp`: QWERTY plus symbols layers, a special row (shift with
  caps lock, #+, cursor left/right, space, delete, OK); hold SELECT = OK;
  RETURN = cancel, asking to discard if the text changed.
- `ui_screens.h/.cpp`: loading screen (moon + "SELENO", hold SELECT = offline
  menu), tracking screen (double RETURN = `panelStopTrigger` + menu; hold
  SELECT = menu over tracking), confirm / message (busy spinner, timeout) /
  choice dialogs, `showMenu` / `showTracking` (deferred while a sub-screen
  is open).
- `ui_host.cpp`: `{"cmd":"ui","action":...}`. Actions: hello, status, menu,
  set, prompt (field `prompt` = its id; kinds text, confirm, message, busy,
  choice; queued if the dialog is busy locally), close_prompt, toast, mode,
  tracking, loading, show, key (`key` + `press`: click/down/up/long),
  ack_stop, settings, screenshot (base64 rgb565be; byte order not yet
  verified), state.
- `ui_tests.h/.cpp`: Jog (hold LEFT/RIGHT), driver status, self test,
  buttons, LEDs (host LED commands refused while it runs), display patterns
  (bars / edges for the panel offsets / gradients / solid colours / fonts),
  IMU, I2C scan, Pi link. `panelMove()` offers to release the E-stop and
  auto-engages the driver.
- `ui_panels.cpp`: firmware tabs Motors(30), Test(40), Display(60),
  About(90). The Pi will send Scope(10), Setup(20), Pi(70).
- `ui_draw.cpp`: helpers. LovyanGFX `drawString` skips characters < 0x20, so
  footer key icons (\x07 SELECT, \x1b RETURN, \x10/\x11/\x1e/\x1f arrows)
  go through `drawIconText`.
- `ui_dialogs.h`, `display_device.h`.

Changed firmware:
- `motors`: `MotionSource` Host/Panel, `PanelMotionScope`,
  `motorsHostMotionFsps`, host hold (refuses Host moves);
  status/motion_done carry `source`.
- `stepper`: `MotionEnd::PanelStop` ("panel_stop", ramps like Stopped),
  `stepperPeakRate`.
- `estop`: `panelStopTrigger` / `panelStopPending` / `panelStopAcknowledge` /
  `panelStopCount`, `returnButtonPressed` (single press when host motion is
  > `kReturnEstopAboveFsps` 50 = E-stop; double press within 500 ms with
  slow host motion and the UI not navigating = panel stop),
  `returnButtonSetNavigates`.
- `front_panel`: RETURN calls `returnButtonPressed` instead of
  `estopTrigger`; button listener for the UI; `ledsSetMuted`.
- `host_link`: `hostLastRxMs`; event lines 384 bytes.
- `display`: `displayUseRotation` / `displayHostRotation` /
  `displayBacklightLevel` / `displayDevice`.
- `commands`: `ui` dispatch; LED commands refused during the LED test.
- `commands_display`: drawing actions call `uiHostDrawing()`.
- `main`: `uiBegin()` after imuBegin, `uiPoll()` in loop, boot event
  `"ui":true`.
- `config.h`: `kReturnEstopAboveFsps`, `kReturnDoublePressMs`.
- **Behaviour change:** a single RETURN no longer E-stops unless a host
  move is slewing. Update `esp32/bringup.py`'s estop check accordingly.

**Next steps, in order:**
1. Flash the devkit (`pio run -e devkit -t upload --upload-port
   /dev/ttyUSB0`; check `fuser /dev/ttyUSB0` first). Check that it boots and
   the `boot` event has `"ui":true`.
2. Add Python helpers: ESP32Connection `ui(...)`, plus a small CLI
   (`esp32/panel_cli.py`: screenshot to PNG via Pillow, key, state). Take
   screenshots and inspect them with the Read tool: loading screen, then
   `ui hello`, menu tabs, each test screen, keyboard, dialogs, tracking
   (`ui tracking state=tracking`). Fix the layout at 128x160 and check the
   screenshot byte order.
3. Test the logic with injected keys and `ui state`:
   - menu navigation and the keyboard;
   - Pi prompts and their result events;
   - double RETURN on the tracking screen with a host `start_continuous`
     running: the motor stops, `ui stop` event, host moves refused until
     `ack_stop`;
   - a single injected RETURN during a fast host move must E-stop.
4. Pi side (not started):
   - `utils/panel_menu.py`: declarative Pi panels. Setup: client_id "Device
     name", base_url "Server", api_token "API key" (secret). Scope: status,
     stop tracking, live view. Pi: IP, Wi-Fi, server state, restart client
     (os.execv; client.service runs Client.py inside `screen`,
     Restart=on-failure), reboot/shutdown (`sudo -n`, show an error if not
     allowed).
   - `utils/panel_ui.py`:
     - hello and the menu on connect and on the `boot` event;
     - `ui status` every 2 s with `clock_s` (local seconds since midnight),
       ip, server, and changed values via `set`;
     - handle `ui` change/action/prompt/stop events: on stop, run
       stop_tracking() then `ack_stop`, also when hello reports
       `panel_stop_pending`;
     - first-run wizard with prompts.
   - Hook it in `utils/esp32_state.set_connection`.
   - `Client.py`: wait for a complete config instead of exiting 78.
   - `websocket.py`: reconnect with the new globals after a config change.
   - `tracking.py`: send `ui tracking` (slewing, tracking with target/ra/dec,
     stopped).
   - `esp32_events`: note `expander_lost`.
   - Add pty tests.
5. Document `ui` in `PROTOCOL.md`. Update this log. Commit only when Alex
   asks.

Ideas not done: a phone/web setup page for the API key (typing it on the pad
is slow); I2C 100 kHz for the BNO085 (still only proposed).

### Menu UI: continued (2026-10-16, after the handoff above)

**Done and tested on the devkit** (flashed; seleno_v08 also builds, no
warnings; RAM 12 %, flash 51.5 %; heap free about 211 KB with the sky view
running):
- Fixed: `extractRequestId` (host_link.cpp) took the first `"id"` in the
  line, so `ui menu` replies echoed a menu item's id and never matched. Now
  it takes only the top-level key.
- Screenshots verified for: loading screen, menu tabs/list, keyboard, the
  confirm/busy/message dialogs, Pi tabs, the wizard and the sky view (byte
  order is rgb565be). Tools:
  - `python -m esp32.panel_cli state|shot out.png|key ...|send '{...}'`;
  - `ESP32Connection.ui()`, `ui_key()`, `ui_state()`, `ui_screenshot()`,
    `ui_sky()`.
- RETURN policy, tested with real host moves:
  - double RETURN on the tracking screen stops the motor, shows the menu,
    sends `ui stop`, and refuses host moves until `ack_stop`;
  - double RETURN in the menu only navigates;
  - a single RETURN during a 2000 sps slew latches the E-stop (source
    button);
  - panel jogs (source panel) are not held.

**Tracking view = sky view.** Alex asked for it to look like the server's
star map (Seleno repo, branch `Server/canary`, `static/js/star_map`), plus
these ideas:
- polar radar (his top pick);
- lock-on reticle;
- star field;
- axis arcs;
- Moon motif;
- text strip.

All API calls are made on the Pi. The domain is `client_config.base_url`;
never hard-code seleno.org. Endpoints:
- `api/stars?maxMag=5&include_planets=true` (1544 stars + 10 Sun/Moon/planets);
- `api/star_names` (HD id -> name, 572);
- `api/constellations` (24 figures: id, name, anchor, line segments);
- `api/planets` (now; about 2 KB).

Design:
- `utils/sky_data.py` fetches and caches to `config/sky_cache.json`
  (catalogue weekly, planets every 10 min; works offline from the cache).
- `utils/sky_view.py` is a port of the star map's maths: GMST/LST,
  eq->horizon matrix, horizon-level camera with rotX = alt and rotY = az,
  centred on the scope, stereographic projection with k2 = (w/2)/tan(fov/4).
  The star map's sizes, magnitude/zoom link and colours are scaled to the
  LCD; labels use an occupancy grid, keep clear of the reticle, radar and
  field-width label, and non-ASCII names are transliterated.
- It encodes a binary SKY1 scene (format in PROTOCOL.md), sent with
  `ui sky` + bytes; it's 5-7 KB, takes about 100 ms to send and 15-110 ms
  to build.
- `ui_sky.cpp` draws:
  - the scene: tint, grids, figures (AA lines), stars and planets, with the
    Moon phased from the Pi's illumination and its bright limb toward the
    Sun;
  - plus overlays: top strip (target, distance, ETA from the motors' remaining
    pulses; LOCK + elapsed when tracking), green reticle, amber lock rings that
    shrink with the error (green and one pulse at lock), an edge arrow for an
    off-screen target, RA/Dec progress arcs, a radar inset (N up, E left,
    fading trail), and the field width;
  - SELECT toggles a details page, LEFT/RIGHT zoom (an event to the Pi),
    UP/DOWN toggle the radar, hold SELECT opens the menu over tracking, and
    double RETURN stops.
  The night theme turns everything red.
- Pi tabs (`utils/panel_menu.py`, declarative: Info/Action/Toggle/Choice/
  Number/Text):
  - Scope: status, Sky view, Stop tracking, Live view, camera, site.
  - Sky: field, horizon/equatorial grids, ecliptic, constellations and
    names, star names, planets, below-horizon; saved under `sky_view` in
    config/client_config.json.
  - Setup: device name, server, API key, status, Run setup again.
  - Pi: IP, Wi-Fi, hostname, server, uptime, restart client (os.execv),
    reboot/shutdown (`sudo -n systemctl`, which needs passwordless sudo).
- `utils/panel_ui.py`:
  - hello on connect and on `boot`, a 2 s status heartbeat, and value
    diffs;
  - events: change/action/prompt/stop (deduped, then stop_tracking +
    ack_stop)/sky zoom;
  - prompts and the first-run wizard (welcome, name, server, key, confirm
    defaulting to Save, connecting, result; cancel asks "Leave setup?");
  - the sky loop: 1 s while slewing, 5 s otherwise, only while the panel
    shows it.
  It's attached from `esp32_state.set_connection`.
- Hooks:
  - `tracking.py` publishes slewing/centering/tracking/idle and the live
    pointing (`_publish_state`, `is_sidereal_tracking`);
  - `websocket.py` has `server_state`, `apply_client_config()` and
    `request_reconnect()`, so new settings apply without a restart;
  - `Client.py` waits for a complete config (panel, file or `Client.py
    setup`) instead of exiting 78;
  - `esp32_events` logs `expander_lost`, and the panel toasts it;
  - `bringup.py`'s estop check covers both RETURN behaviours, and its lcd
    check hands the screen back to the menu.
- Tests:
  - new `esp32/tests/test_panel_ui.py` (15 tests: sky maths/encoding
    round-trip with a SKY1 decoder that mirrors ui_sky.cpp, and PanelUI
    against a fake connection with temporary config files) passes;
  - the old pty tests pass (12/12).
- PROTOCOL.md: the RETURN policy, the panel stop, the UI commands and the
  SKY1 format.

**Not done or not verified:**
- Client.py hasn't been run end to end against the real server; panel_ui
  was tested with the devkit plus a harness.
- Real buttons, LCD colours and orientation, and the backlight need the
  board.
- Alex committed all of the above himself as `09fa190` ("new esp32
  firmware").

## Feature: offline mode and manual control (Alex, 2026-10-16)

Alex asked for:
- manual control of the telescope without a server;
- a "use offline" button in the setup (name / server / API key) and a
  toggle in settings once it's set up;
- typing an RA and Dec to slew to and track, with no server;
- in offline mode, photos saved to a local directory.

**Done (uncommitted on `firmware/v0.8`, on top of `09fa190`):**
- Offline mode is `client_config.offline` in config/client_profile.json
  (`config_state.is_offline_mode()` / `set_offline_mode()`).
  - `Client.py` doesn't wait for server settings when it's on, and skips
    the IP location lookup.
  - `websocket.py`: `run_client` and `send_frames` wait while
    `server_enabled()` is false (offline, or settings missing), and
    `server_state` says "offline" / "not set up". `websocketClient`
    accepts a config without server settings when offline.
  - `sky_data.py` never fetches offline; planet positions over 6 h old
    aren't drawn (the Moon drifts).
- Panel (`utils/panel_ui.py`, `utils/panel_menu.py`):
  - The wizard's welcome is now a confirm: Connect / Offline. Backing out
    while settings are missing offers offline (instead of "Leave setup?").
    Finishing the wizard turns offline off.
  - Setup tab: "Offline mode" toggle. Off with missing settings starts the
    wizard; off with settings applies them and connects; on drops the
    sockets.
  - `run_flow(name, fn)`: prompt sequences run on their own thread, one at
    a time (the wizard uses it too).
  - `only_when(item, condition)`: an item's `visible` is sent with the
    value diffs (`ui set` panel/item/fields). Live view is hidden offline.
- Manual control (`utils/manual_control.py`), Scope tab:
  - Go to RA/Dec: keyboard prompts (RA in hours `5 35 17`, `5:35:17`,
    `5h35m17s`, `5.588`, or degrees with `d`; Dec `-5 23 28` etc.),
    "Go there?" with the altitude, then checks: within 15 deg of the Sun
    (computed locally, `sky_view.sun_radec`), below the horizon, E-stop
    latched (offers to release). Then `show_sky` + `trackCoordinates`.
    Target name is short for the strip: `5h35 -5`.
  - Go to star: the 30 brightest named stars at least 10 deg up, from the
    cached catalogue.
  - Align on star: pick the star you've centred by hand; then
    `tracking.sync_position()` (stop, zero the step counters, set the
    position) and track it.
  - Site: a Text item, "lat, lon" (also `51.5N 2.6W`); blank = back to
    GPS/IP. A manual site (`source: manual`) survives startup's IP lookup;
    a hardware GPS fix still replaces it.
- Photos (`utils/photos.py`), new Camera tab: Take photos (press again to
  stop after the current one), Shots 1-50, files on the Pi, path, and live
  view (online only). Saved to `photos/offline/<night>/` (the night = the
  date the evening started; `photos.local_dir` in client_config.json
  overrides), with `photos.jsonl` recording time, target, RA/Dec and site.
  Panel photos are always local; server-requested photos upload as before.
  `Camera.capturePhoto` takes `save_folder`. LED calls in the photo run are
  best effort (the firmware refuses LED commands during its LED test).
- Firmware: a text item's `hint` is shown under the keyboard title
  (ui_menu.cpp; PROTOCOL.md updated). Flashed to the devkit; both envs
  build clean.
- Also fixed:
  - sky labels overlapped (the grid only reserved the anchor cell); labels
    now reserve their whole box;
  - `handlers.capturePhoto` uploaded to the server read at import, so a
    server changed on the panel needed a restart;
  - panel text avoids brackets (DejaVu9 draws them like "!").
- Tests: test_panel_ui.py has 35 tests (manual control parsing, star list,
  Sun, photo runs, flows against the fake connection, offline toggle,
  first-run offline). A module fixture stops tests reaching a real serial
  port: the LED manager used to find the devkit. test_interface_pty 12/12.
- Devkit checks (harness with temporary config copies):
  - first-run Offline;
  - the tabs;
  - typed RA/Dec with a bad value, then the slew;
  - go to star with the E-stop release;
  - toggling offline off reopens setup.

  `Client.py` also ran end to end offline from a scratch copy: no server
  attempts, panel attached, location kept.

**Not done or not verified:**
- No camera was connected, so `take_photo` has only been tested with a fake
  capture.
- Going from offline back online hasn't been run against the real server.
- Photos taken with the camera's own shutter button stay on its card; only
  panel photos are downloaded.
- Possible next steps:
  - upload offline photos when back online;
  - nudging the scope with the arrows while tracking (the firmware's jog
    stops sidereal tracking);
  - exposure settings from the panel.

## Feature: Sun protection (Alex, 2026-10-16)

Committed before this: `06e8448` (offline mode, manual go-to, local
photos).

Alex asked for:
- an on-screen warning within 30 deg of the Sun;
- within 20 deg, a random 4-press code of up/down/left/right, shown on
  screen, to be entered first;
- a message about the dangers to eyes and equipment.

**Done (committed by Alex as `50a9e25`; the next section changes parts of it):**
- `utils/sun_guard.py`, called at the top of
  `tracking.trackCoordinates`, so it covers go-tos from the panel and the
  server. It runs before the previous tracking is stopped and before
  anything moves; a refusal returns `{"error": ...}` to the caller.
  - The Sun is computed locally (`sky_view.sun_radec`), so it works
    offline. It applies whether or not the Sun is up.
  - Distance is the closest approach: at the target or on the slew. The
    slew model matches `_move_motors`: RA and Dec move together at the
    same step rate (so Dec goes 2.5x faster in sky degrees at 360:1 and
    144:1), and RA takes the short way. It starts from the dead-reckoned
    position.
  - Within 30 deg: a confirm with `style: warn` (amber bar with a Sun
    symbol) about eyes, the finder and cameras. Cancel is selected first.
  - Within 20 deg: a `style: danger` notice (red bar) about blinding and
    burnt cameras and eyepieces, then a `code` prompt. The code screen
    says "only with solar filters on the telescope and finder". The ESP32
    picks the code and the screen times out after 90 s.
  - No panel attached: a warning is logged and the go-to goes ahead;
    within 20 deg is refused ("confirm on the telescope's front panel").
  - A yes is remembered for that target for 2 minutes, used once. The
    panel flows (`manual_control._sun_ok`) ask first, before the horizon
    and E-stop prompts, and trackCoordinates then finds the answer.
    trackCoordinates' own asks aren't remembered. Align on star checks
    with no slew.
  - The old 15 deg check in manual_control is gone. Panel go-tos no longer
    call `show_sky`: the firmware opens the sky view itself when the slew
    starts.
- Firmware:
  - `Alert` (None/Warn/Danger), `drawAlertBar` and `drawSun` (ui_draw.cpp).
  - The confirm takes an alert (`style` in `ui prompt`).
  - New `CodeScreen` (ui_screens.cpp, `openCode`): four arrow boxes (green
    when done, the next one outlined), "Wrong key: new code" with a fresh
    code, and a seconds countdown.
  - The kinds are documented in PROTOCOL.md.
- Tests: 43 in test_panel_ui.py (levels, the slew path, warn, danger and
  code, cancel, no panel, trackCoordinates refusing before it stops
  anything, panel flows with the Sun pinned).
  - The test setup now writes all three config files before switching to
    them. Leftover threads from earlier tests used to rebuild a blank
    profile and start the wizard, which made the tests flaky.
  - It also stops tracking's hour-angle thread, which had been writing to
    the real runtime_state.json during tests.
- Devkit:
  - the warning (Go on);
  - the danger notice for a slew passing the Sun, with a long name;
  - the code: a wrong key draws a new one, the right code goes ahead;
  - the night theme, and RETURN cancels.
  All the text fits without scrolling. Both firmware envs build clean.

**Not covered (by design or not yet):**
- Raw motor commands from the server (`espMoveSteps`, `espStart`,
  `espTurnDegrees`, ...) and the panel's Jog test move the motors with no
  sky position, so the guard can't see them.
- The tracking loop's small centring corrections aren't checked; they head
  for a target that was already approved.
- The path check trusts the dead-reckoned position; if that's wrong, so is
  the path.

## Feature: routes round the Sun, over the poles (Alex, 2026-10-16)

Alex asked:
- make sure nothing can track an object in the exclusion zone without the
  check;
- when a slew would pass through the zone, navigate smoothly round it;
- slews should be efficient, so they may go over the zenith/nadir if
  needed (read as: the Dec axis may go past either pole when that's the
  quicker way).

**Done (uncommitted, on top of `50a9e25`):**
- Firmware `path` command: both motors along a polyline (up to 63 points,
  logical steps from now) as one smooth move.
  - The plan is pure logic in `logic/path.h`:
    - a segment's length is its larger axis distance, so speed is for the
      faster motor;
    - the speed through each corner is limited so neither motor's speed
      jumps by more than the start speed;
    - a backward braking pass, then acceleration at runtime.
  - `stepper.cpp` path mode:
    - the 1 kHz ramp task moves a cursor along the path and steers each
      axis to its point (feed-forward speed plus 20/s times the error);
    - the ISR can reverse DIR mid-move, waiting a tick before stepping;
    - stop slows down along the path; E-stop is immediate;
    - it ends with Complete once both axes settle on the last point.
  - `motorsStartPath` picks each axis's microsteps for its peak speed on
    the path and runs the usual checks (E-stop, front-panel hold, engaged,
    not moving).
  - Status shows `path_progress`; the sky view's arcs and ETA follow paths.
  - Devkit:
    - a 24-point half circle held about 1600 sps (the cruise speed)
      throughout, reversing Dec smoothly, and ended exactly on target;
    - corners and full reversals land exactly;
    - stop ramps; E-stop is immediate.
  - Both envs build clean (RAM 13.8 %). The native env has no host
    compiler on this Pi (`g++` isn't installed), so `path.h` was tested on
    the devkit, not in `pio test`.
- `core/hardware/mount.py` (pure):
  - Axis angles: RA axis from the meridian, Dec axis from the equator,
    unwrapped; past ±90 the mount is flipped (`sky_from_axes`,
    `axis_solutions`, `nearest_solution`).
  - Limits are `mount` in client_config.json: RA ±270, Dec ±180 and
    `pole_crossing` true by default.
  - `plan_route` tries every way of pointing at the target (normal or
    flipped, every turn within the limits), quickest first. It goes
    straight if that keeps clear of the Sun. Otherwise it takes the
    shortest way round the keep-out: the Sun's circle mapped into the axis
    plane scaled to motor turns, a convex hull every 15 deg, then Dijkstra
    over a visibility graph. The result is checked along the route; if it
    fails, the outline is padded 1.5/3/5 deg and tried again.
  - Results:
    - turns stay under ~11 deg, so the firmware keeps cruise speed;
    - RA is geared 2.5x slower, so going round usually costs no time;
    - if limits leave no way round, it goes straight and says so;
    - planning takes 1-3 ms.
- Position model (`utils/telescope_state.py`):
  - The axis angles (`axis_ra_deg`, `axis_dec_deg`) are now the record.
    `get_telescope_coords` derives RA/Dec from them and the time, so a
    stopped scope's RA drifts as it should, and the old
    `_last_ha_baseline` hack is gone.
  - `set_telescope_coords` (syncs) picks the axes on the side the mount is
    on.
  - Older state files are converted from their RA/Dec at `updated_at`,
    assuming the mount held still since.
- `tracking.py`:
  - `trackCoordinates` order:
    1. check the site;
    2. run the Sun guard (it plans the route);
    3. stop the old session;
    4. bring the axes up to date from the step counters, after waiting
       for motion to stop;
    5. `plan_goto`, aiming where the target will be on arrival;
    6. `_run_route` sends `path`.

    It returns `{status, seconds, round_the_sun, over_the_pole,
    closest_to_sun_deg}` or `{error}`.
  - Centring corrections go through the planner (at refine speed when
    small).
  - Step counter baselines are re-read when the motors are initialised,
    and jogs between sessions are now counted.
  - A watchdog in the loop stops tracking and toasts the panel if:
    - the pointing or the target gets closer to the Sun than the session's
      floor (on the way: min(19.5, the route's closest - 1); once locked:
      19.5, or the approved target's distance - 1);
    - an axis goes more than 2 deg past its limit.
- Sun guard changes:
  - The closeness on the way comes from the planned route, and moving away
    from the Sun doesn't count.
  - The warning is for targets under 30 deg. The code is for getting
    under 20 deg, on the way or at the target, so a detour passing at
    about 23 deg asks nothing.
  - Keep-out: `keep_out_deg` = min(22, start - 0.5, target - 0.5).
  - The global yes cache is replaced by a `SunApproval` object that the
    panel flows pass to `trackCoordinates(..., sun_approval=)` (keyword
    only). JSON from the server can't create one, and a dict is ignored.
- Tests (51 in test_panel_ui.py):
  - mount maths and routes;
  - `trackCoordinates` against a `FakeMount`: a curved route, over the
    pole, and the watchdog;
  - the approval rules.

  test_interface_pty 12/12. Stable over repeated runs.
- Devkit end to end (scratch config copies, slew speed 4800):
  - Curved: round the real Sun, 6 points, 33 s; its dead-reckoned pointing
    stayed 23.4 deg or more from the Sun. It landed 0.014 deg off, made one
    correction, then sidereal tracking (213.9 pulses/s).
  - Over the pole: Dec axis 75 to 105 in 4 s (instead of 180 deg of RA),
    then tracking on the flipped side.

**Not covered / to check on the real mount:**
- The default limits (RA ±270, Dec ±180) allow swinging the tube under the
  south pole. Check the tube, camera and cables clear the tripod and pier
  before relying on it; narrow `mount` in client_config.json if not.
- Raw motor commands from the server (`espStart`, `espMoveSteps`, ...) and
  the panel's Jog test still bypass the planner and the guard.
- (Tested since: a double RETURN during a slow path stops both motors
  without an E-stop, shows the menu, and refuses host paths until
  `ack_stop`.)
- Whether the mount is flipped can't be sensed: a sync takes the side it
  was last on. After moving it by hand, use Align on star.
