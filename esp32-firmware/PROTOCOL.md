# SELENO v0.8 host protocol

Newline-delimited JSON between the Pi and the ESP32 at 921600 baud, 8N1.

- **v0.8 board:** ESP32 UART0 ↔ J7 ↔ Pi GPIO14/15 (`/dev/ttyAMA0` on a Pi 5,
  `/dev/serial0` on a Zero 2 W).
- **Dev kit:** UART0 is its USB chip (`/dev/ttyUSB*`). The `devkit` build
  also listens on a second UART (RX IO26, TX IO25). Wire it to the Pi's
  GPIO14/15 to test the GPIO-UART path while USB stays connected.

The Pi side is `esp32/interfaceESP32.py`. Hardware facts come from
`SELENO Controller v0.8 Hardware Report.txt`.

## Framing

- **Request:** one JSON object per line with `"cmd"`, plus optional `"id"`
  (a number or a short string). The firmware echoes `id` in the reply, so a
  late reply can't be mistaken for the next one. Requests without `id` (old
  hosts) still work.
- **Reply:** `{"status":"ok","id":7,"data":{...}}` or
  `{"status":"error","id":7,"message":"..."}`. A reply goes back on the port
  the request came in on.
- **Event:** an unsolicited line with an `"event"` member. Events go to
  every port that has sent at least one command. UART0 always gets them.
- **Binary payloads:** `display blit` and `display store`/`store_begin` are
  followed by exactly `length` raw bytes. If the bytes stop for 5 s, the
  transfer is dropped and an error reply is sent.
- **Boot noise:** after a reset the ROM prints a banner at 115200 baud, which
  is unreadable at 921600. Skip anything that isn't JSON.
- **TX pacing:** the firmware sends replies in 32-byte bursts with a 500 µs
  gap. Without it, a dev kit's CH340 dropped bytes from 53 % of 300-byte
  replies at 921600. Change it with `host set`.

## General

| Command | Fields | Reply data |
|---|---|---|
| `ping` | | `fw`, `board`, `devkit`, `uptime_ms`, `port` |
| `info` | | also `build`, `reset_reason`, heap, `ports`, `estop`, `motors[]` (axis, driver_present, enabled, microsteps), `expander_present`, `imu_present`, `display_initialized`, `storage_mounted`, `dropped_events`, `i2c_errors` |
| `reboot` | | ok, then the ESP32 restarts (`boot` event) |
| `host` | `action`: `status` (default) / `set`; `tx_chunk` (bytes, 0 = no pacing), `tx_gap_us` | `baud`, `tx_chunk`, `tx_gap_us`, `port`, `ports` |
| `estop` | `action`: `trigger` / `clear` / `status` | `active`, `source` |
| `stop_all` | | ok (every axis stops; nothing is latched) |
| `i2c_scan` | | `devices`: list of addresses (0x20 = MCP23017, 0x4A/0x4B = BNO085) |
| `panel` (alias `buttons`) | `action`: `status` / `reinit` | `expander_present`, `buttons{}`, `leds{}`, `raw_gpio`, `interrupts`, `int_pin_low`, `expander_resets` |
| `led` | `led`: `board`/`red`/`green`/`white`/`blue`/`yellow`; `mode`: `on`/`off`/`toggle`/`blink` (+`interval_ms`, `auto_off_ms`), or `on`: bool | ok |
| `imu` | `action`: `status` / `read` / `enable` / `disable` / `reinit` | status: `present`, `address`, `firmware`, `events`, `errors`, `resets`. read: `rotation_vector` and `game_rotation_vector` (quaternion `i,j,k,real`, `accuracy`, `age_ms`, `euler_deg{yaw,pitch,roll}`), `gravity` and `magnetic_field` (`x,y,z`) |

## Emergency stop

The firmware triggers the stop on its own when RETURN is pressed, acting on
the first low reading before debounce. The host can also trigger it.

Triggering does three things:
- stops step generation on both axes immediately;
- latches the stop;
- sends `{"event":"estop","active":true,"source":"button"|"host"}`.

The drivers stay enabled, so the mount holds position instead of swinging.
While latched, `turn_degrees`, `move_steps` and `start_continuous` reply
`Emergency stop active`. Only `{"cmd":"estop","action":"clear"}` releases
it, and that sends `{"event":"estop","active":false}`.

## Motors

The board has two fixed axes, registered at boot and disengaged:

| id | axis | driver | STEP/DIR/EN | connector | TMC UART address |
|---|---|---|---|---|---|
| `motor1` (RA) | 0 | U7 | 17 / 16 / 21 | J1 | 0 |
| `motor2` (DEC) | 1 | U8 | 23 / 22 / 27 | J2 | 1 |

Every motor command needs `"motor": "<id>"`.

### Units

Positions and speeds are in **logical steps**: `steps_per_rev` per motor
revolution, 1600 by default (1/8 steps, as the old firmware used). The
logical unit doesn't depend on the driver's actual microstep setting.
Internally the position is kept in 1/256 microsteps (51200 per revolution),
so changing the microstep setting never loses position. `position` is the
floor of that value in logical steps; `position_fine` is the exact
1/256-microstep count.

### Automatic microsteps and chopper (the defaults)

- **Microsteps `auto`.** When a move starts, the firmware picks the finest
  power of two in [`min_microsteps`, `max_microsteps`] (default 4–256)
  whose STEP pulse rate stays within `max_pulse_hz` (default 5000). It
  judges by the fastest speed the move actually reaches, given its distance
  and the acceleration. Typical results at 1600 steps/rev:

  | Move | Microsteps |
  |---|---|
  | Sidereal tracking (6.7 sps), 150 sps refine | 1/256 |
  | Short nudges at slew speed | 1/64 |
  | 2000 sps slew | 1/16 |
  | 6000 sps | 1/8 |
  | 9000 sps | 1/4 |

  The change is one CHOPCONF write, made while the axis is idle. A coarse
  move can end up to half a physical step from its target; `position_fine`
  shows exactly where it ended.
- **Chopper `auto`.** TMC hybrid mode: StealthChop (quiet, smooth) below
  200 full steps/s (1 rev/s), SpreadCycle (torque) above. The switch-over
  register value doesn't depend on the microstep setting.
- **Acceleration.** A move starts at up to `start_sps` (default 400 =
  0.25 rev/s) with no ramp, then accelerates at `accel_sps2` (default
  3200 = 2 rev/s²). It decelerates so it reaches the start speed on its
  last pulse, and always lands on its exact pulse count. `stop` ramps down.
  The emergency stop, driver faults and `disengage` stop at once. Tracking
  and refine speeds are below the start speed, so they start instantly.

### Commands

| Command | Fields | Notes |
|---|---|---|
| `list_motors` | | `motors`: registered ids |
| `create_motor` | `motor`; `step`,`dir`,`en` (must match an axis) or `axis`; optional `steps_per_rev`, `microsteps` (number or `"auto"`), `speed_us`/`sps`, `engage` | Binds or reconfigures; replies with `status` data |
| `delete_motor` | | Stops, disengages and frees the axis |
| `engage` / `disengage` / `enable` (`value`) | | Real board: engage fails if the driver didn't answer on UART |
| `set_speed` | `sps` or `speed_us` (logical) | A running move ramps to the new speed |
| `turn_degrees` | `degrees`, `forward` | Negative degrees reverse |
| `move_steps` | `steps` (signed, logical) | |
| `start_continuous` | `forward` | Runs until `stop`; the sidereal tracking path |
| `stop` | | Ramps down from above the start speed, otherwise immediate |
| `status` | | `enabled`, `moving`, `position`, `position_fine`, `steps_per_rev`, `speed_sps`, `speed_us`, `accel_sps2`, `start_sps`, `pulses_remaining` (−1 = continuous), `pulse_rate` (cruise), `pulse_rate_now`, `microsteps` (driver), `microstep_mode` (`auto`/`fixed`), `auto{}`, `chopper`, `chopper_threshold_fsps`, `driver_present` |
| `get_position` | | `position`, `position_fine`, `steps_per_rev`, `moving` |
| `reset_position` / `set_position` (`position`, logical) | | |
| `set_microsteps` | `microsteps`: `"auto"` or 1–256 (power of two) | A fixed value applies now (refused while moving) and turns auto off |
| `set_auto` | `min_microsteps`, `max_microsteps`, `max_pulse_hz` | Tunes automatic selection |
| `set_accel` | `accel` (logical steps/s², 0 = no ramp), `start_sps` | Applies to a running move too |
| `set_steps_per_rev` | `steps_per_rev` (200 × power of two) | Changes the logical unit; position keeps its angle, speeds keep their angular speed |
| `set_chopper` | `mode`: `auto` / `stealthchop` (`stealth`, `silent`) / `spreadcycle` (`spread`) / `hybrid` + `hybrid_threshold_sps` (logical) or `hybrid_threshold_fsps` (full steps/s) | |
| `set_current` | `run_ma` (1–1350), `hold_multiplier` (0–1) or `hold_ma` | Replies with `driver status` |
| `driver` | `action`: `status` (default) / `reinit` / `set` (`interpolate`, `stall_threshold` 0–255, `invert_direction`) / `read_reg` (`reg`) / `write_reg` (`reg`, `value`) | status: `present`, `version` (0x21), `configured`, `enabled`, `diag`, `settings{}` (incl. `chopper_threshold_fsps`, `tpwmthrs`), and `live{}` (`ifcnt`, `gstat_*`, chip `microsteps`, `irun`, `ihold`, `rms_current_ma`, `tstep`, `sg_result`, `mscnt`, `drv_status{...}`) |

Other defaults: 1.0 A run current, 0.5 A hold current, 256-step
interpolation, StallGuard off.

### Step timing

Step pulses come from one 20 kHz timer interrupt with a 32-bit phase
accumulator per axis, so the long-run rate is exact to about 5 µsteps/s.
A 1 kHz ramp task adjusts the rate during acceleration. The maximum pulse
rate is 9999/s. Measured on the dev kit: 6.68 sps for 10.1 s gave 68
steps; a 360° slew at 2000 sps with ramps took 1.23 s and landed on
exactly 51200 fine units.

## Display (`"cmd":"display"`, field `action`)

LovyanGFX `Panel_ST7735S`, 128 × 160, RGB565. Colours are `"RRGGBB"` hex.
Call `init` first; until then drawing is ignored.

| Action | Fields |
|---|---|
| `init`, `status`, `power` (`on`), `backlight` (`brightness` 0–255), `rotation` (`rotation` 0–3) | |
| `clear` | `color` |
| `fill_rect`, `draw_rect` | `x,y,w,h,color` |
| `draw_line` | `x0,y0,x1,y1,color` |
| `draw_circle`, `fill_circle` | `x,y,r,color` |
| `draw_pixel` | `x,y,color` |
| `set_cursor` (`x,y`), `set_text_color` (`color`), `set_bg_color` (`color`), `set_text_size` (`size`) | |
| `text` (alias `print`) | `text`, optional `x,y,size,color,bg,newline` |
| `blit` | `x,y,w,h`, `format`:`RGB565`, `length` = w·h·2, then that many bytes (big-endian RGB565). With `"compress":"zlib"` and `uncompressed_length`, `length` is the zlib size |
| `store_begin` | `name`, `length`. Replies ok, then takes the bytes, then replies ok again (same id) |
| `store` | Like `store_begin`, but only the final reply |
| `play` | `name`, `x,y,w,h`: a raw RGB565 file from LittleFS |
| `play_sequence` | `files_csv`, optional `durations_csv` (ms, default 100) |
| `play_sequence_prefix` | `prefix`, `count`, `start`, `pad` → `<prefix><index>.rgb` |
| `list_files`, `storage_info`, `delete_file` (`name`), `format_storage` | |

## Events

| Event | Members | When |
|---|---|---|
| `boot` | `fw`, `board`, `devkit`, `reset_reason`, `drivers[]`, `expander` | After every start |
| `estop` | `active`, `source`, `already_latched` | Trigger and clear |
| `button` | `button` (`select`, `return`, `top`, `bottom`, `left`, `right`), `action` (`press`, `release` with `held_ms`, `hold` after 1 s), `t` | Debounced 15 ms |
| `motion_done` | `motor`, `axis`, `reason` (`complete`, `stopped`, `estop`, `driver_fault`, `disengaged`), `position`, `position_fine` | A move ends |
| `driver_fault` / `driver_fault_cleared` | `motor`, `axis`, `drv_status{}` | DIAG pin rises or falls; the axis stops on a fault |
| `driver_temperature` | `motor`, `drv_status{}` | `otpw` or `ot` changes |
| `driver_reset` | `motor` | Driver brown-out; it has been reconfigured and the axis stopped |
| `driver_lost` / `driver_found` | `motor` | Driver stops or starts answering on UART |
| `expander_found` / `expander_reset` / `expander_lost` | `device` | MCP23017 |
| `imu_found` / `imu_lost` / `imu_reset` | `device`, `address` | BNO085 |

The Pi interface also generates `link_lost` itself, when the serial port
fails.
