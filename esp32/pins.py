from __future__ import annotations

from typing import Any, Dict


# Single source of truth for ESP32-side hardware assignments used by the Python
# client. SELENO v0.8 board; every pin is from the KiCad netlist (see
# "SELENO Controller v0.8 Hardware Report.txt", "Board pin header"). The
# firmware has the same map in esp32-firmware/src/board_pins.h.
ESP32_HARDWARE: Dict[str, Any] = {
    "board": "seleno-v0.8",
    "host_link": {
        # UART0 on the ESP32. v0.8: J7 -> Pi GPIO14/15. Dev kit: its USB chip.
        "baudrate": 921600,
        # Pi GPIOs driving the auto-program pair (Q3 -> EN, Q2 -> IO0).
        "reset_gpio": {"dtr": 18, "rts": 23},
    },
    "leds": {
        # IO2: the dev kit's onboard LED; unconnected on v0.8.
        "board": {"channel": "board", "pin": 2},
        # Through the MCP23017 (U1) outputs, not ESP32 GPIOs.
        "yellow": {"channel": "yellow", "expander": "GPA0"},
        "blue": {"channel": "blue", "expander": "GPA1"},
        "white": {"channel": "white", "expander": "GPB7"},
        "green": {"channel": "green", "expander": "GPB6"},
        "red": {"channel": "red", "expander": "GPB5"},
    },
    "buttons": {
        # MCP23017 inputs. RETURN is also the firmware's emergency stop.
        "select": "GPA2",
        "return": "GPB0",
        "top": "GPB1",
        "bottom": "GPB2",
        "left": "GPB3",
        "right": "GPB4",
    },
    "display": {
        "width": 128,
        "height": 160,
        "pins": {
            "sck": 14,
            "mosi": 13,
            "dc": 15,
            "res": 4,
            "cs": -1,
            "bl": 5,
        },
    },
    "motor_boards": {
        "ra": {
            "motor_id": "motor1",
            "label": "RA motor",
            "driver": "U7 (TMC2209, UART address 0), connector J1",
            "pins": {"step": 17, "dir": 16, "en": 21},
            # Positions/speeds in 1600 steps/rev, as before. The driver's
            # microsteps and chopper are chosen by the firmware per move:
            # 1/256 + StealthChop when tracking, down to 1/4 + SpreadCycle
            # when slewing.
            "steps_per_rev": 1600,
            "microsteps": "auto",
            "chopper": "auto",
            "engage": True,
            "replace": False,
            "required": True,
        },
        "dec": {
            "motor_id": "motor2",
            "label": "DEC motor",
            "driver": "U8 (TMC2209, UART address 1), connector J2",
            "pins": {"step": 23, "dir": 22, "en": 27},
            "steps_per_rev": 1600,
            "microsteps": "auto",
            "chopper": "auto",
            "engage": True,
            "replace": False,
            "required": False,
        },
    },
    "i2c": {
        "sda": 33,
        "scl": 32,
        "devices": {"mcp23017": 0x20, "bno085": 0x4A},
    },
}


def get_led_channels() -> Dict[str, Dict[str, Any]]:
    return ESP32_HARDWARE["leds"]


def get_display_config() -> Dict[str, Any]:
    return ESP32_HARDWARE["display"]


def get_motor_boards() -> Dict[str, Dict[str, Any]]:
    return ESP32_HARDWARE["motor_boards"]


def get_motor_board_by_id(motor_id: str) -> Dict[str, Any] | None:
    for board in ESP32_HARDWARE["motor_boards"].values():
        if board.get("motor_id") == motor_id:
            return board
    return None


def get_host_link_config() -> Dict[str, Any]:
    return ESP32_HARDWARE["host_link"]
