from interfaceESP32 import ESP32Connection, ESP32Motor
from esp32.pins import get_motor_board_by_id

# Auto-detect available USB port
conn = ESP32Connection()

motor_cfg = get_motor_board_by_id("motor1")
if not motor_cfg:
    raise RuntimeError("motor1 configuration missing in utils/esp32_pins.py")

pins = motor_cfg.get("pins", {})

motor = ESP32Motor.create(
    conn=conn,
    motor_id="motor1",
    step_pin=int(pins["step"]),
    dir_pin=int(pins["dir"]),
    en_pin=int(pins["en"]),
    steps_per_rev=int(motor_cfg.get("steps_per_rev", 1600)),
    engage=bool(motor_cfg.get("engage", True)),
    replace=bool(motor_cfg.get("replace", True)),
)

motor.disengage()