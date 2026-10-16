#!/bin/bash

# Flash a dev kit over USB:   ./flash.sh [env] [port]
#   env  defaults to "devkit"; port defaults to PlatformIO's auto-detect.
# The SELENO v0.8 board has no USB: flash it from the Pi over J7 with
#   ../venv/bin/python -m esp32.seleno_flash write --build-dir .pio/build/seleno_v08
# Stop client.service / Client.py first; it holds the serial port.
ENV="${1:-devkit}"
PORT_ARGS=()
if [ -n "$2" ]; then
  PORT_ARGS=(--upload-port "$2")
fi

echo "🔌 Flashing the '$ENV' build to the ESP32..."

# PlatformIO's own install isn't on the PATH on this Pi.
PIO="$(command -v platformio || command -v pio || echo "$HOME/.platformio/penv/bin/pio")"
if [ ! -x "$PIO" ]; then
  echo "❌ Error: PlatformIO not found (looked on PATH and in ~/.platformio/penv/bin)."
  exit 1
fi

# Optional: Check available memory
AVAILABLE_MEM=$(free -m | awk '/^Mem:/ { print $7 }')
if [ "$AVAILABLE_MEM" -lt 100 ]; then
  echo "⚠️ Warning: Low available memory (${AVAILABLE_MEM}MB). Compilation may fail."
fi

# Timestamped log
echo "$(date): Starting firmware upload..." >> flash_log.txt

# Run the upload with timeout and verbose output
timeout 90s "$PIO" run -e "$ENV" -t upload "${PORT_ARGS[@]}"
UPLOAD_STATUS=$?

if [ $UPLOAD_STATUS -eq 0 ]; then
  echo "✅ Flash successful."
  echo "📡 Launching serial monitor..."
  "$PIO" device monitor -b 921600 "${PORT_ARGS[@]/--upload-port/--port}"
elif [ $UPLOAD_STATUS -eq 124 ]; then
  echo "⏱️ Upload timed out after 90 seconds. Check your ESP32 connection or port settings."
else
  echo "❌ Flash failed with exit code $UPLOAD_STATUS."
  echo "💡 Tip: Check your USB connection, board settings, or code for errors."
fi
