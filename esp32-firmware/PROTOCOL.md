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

| Command | Fields | Notes |
|---|---|---|
| `list_motors` | | `motors`: registered ids |
| `create_motor` | `motor`; `step`,`dir`,`en` (must match an axis) or `axis`; optional `microsteps` or `steps_per_rev` (= 200 × microsteps), `speed_us`/`sps`, `engage` | Binds or reconfigures; replies with `status` data |
| `delete_motor` | | Stops, disengages and frees the axis |
| `engage` / `disengage` / `enable` (`value`) | | Real board: engage fails if the driver didn't answer on UART |
| `set_speed` | `sps` or `speed_us` | Changes a running move too. Max 9999 sps |
| `turn_degrees` | `degrees`, `forward` | Negative degrees reverse. Sub-step moves are a no-op |
| `move_steps` | `steps` (signed) | |
| `start_continuous` | `forward` | Runs until `stop`; the sidereal tracking path |
| `stop` | | Ends the move at the next 50 µs tick |
| `status` | | `enabled`, `moving`, `position`, `steps_remaining` (−1 = continuous), `speed_sps`, `speed_us`, `microsteps`, `steps_per_rev`, `driver_present`, `chopper` |
| `get_position` | | `position`, `steps_per_rev`, `moving` |
| `reset_position` / `set_position` (`position`) | | |
| `set_microsteps` | `microsteps` (1–256, power of two) or `steps_per_rev` | Refused while moving. Position and speed are rescaled so angles stay the same |
| `set_chopper` | `mode`: `stealthchop` (`stealth`, `silent`) / `spreadcycle` (`spread`) / `hybrid` + `hybrid_threshold_sps` | Hybrid uses StealthChop below the threshold and SpreadCycle above it (TPWMTHRS) |
| `set_current` | `run_ma` (1–1350), `hold_multiplier` (0–1) or `hold_ma` | Replies with `driver status` |
| `driver` | `action`: `status` (default) / `reinit` / `set` (`interpolate`, `stall_threshold` 0–255, `invert_direction`) / `read_reg` (`reg`) / `write_reg` (`reg`, `value`) | status: `present`, `version` (0x21), `configured`, `enabled`, `diag`, `settings{}`, and `live{}` (`ifcnt`, `gstat_*`, chip `microsteps`, `irun`, `ihold`, `rms_current_ma`, `tstep`, `sg_result`, `mscnt`, `drv_status{otpw, ot, s2ga, s2gb, s2vsa, s2vsb, ola, olb, t120, t143, t150, t157, cs_actual, stealth, standstill}`) |

Defaults: 1/8 microsteps (1600 steps/rev), StealthChop, 1.0 A run current,
0.5 A hold current, 256-step interpolation, StallGuard off.

### Step timing

Step pulses come from one 20 kHz timer interrupt with a 32-bit phase
accumulator per axis, so the long-run rate is exact to about 5 µsteps/s.
Measured on the dev kit: 6.68 sps for 10.1 s gave 68 steps.

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
| `motion_done` | `motor`, `axis`, `reason` (`complete`, `stopped`, `estop`, `driver_fault`), `position` | A move ends |
| `driver_fault` / `driver_fault_cleared` | `motor`, `axis`, `drv_status{}` | DIAG pin rises or falls; the axis stops on a fault |
| `driver_temperature` | `motor`, `drv_status{}` | `otpw` or `ot` changes |
| `driver_reset` | `motor` | Driver brown-out; it has been reconfigured and the axis stopped |
| `driver_lost` / `driver_found` | `motor` | Driver stops or starts answering on UART |
| `expander_found` / `expander_reset` / `expander_lost` | `device` | MCP23017 |
| `imu_found` / `imu_lost` / `imu_reset` | `device`, `address` | BNO085 |

The Pi interface also generates `link_lost` itself, when the serial port
fails.
