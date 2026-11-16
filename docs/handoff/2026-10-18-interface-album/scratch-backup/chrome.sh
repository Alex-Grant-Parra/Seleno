#!/bin/bash
# Headless Google Chrome unpacked in the scratchpad (not installed)
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/gchrome/root/opt/google/chrome/chrome" --headless=new --no-sandbox --disable-gpu \
  --user-data-dir="$DIR/chrome-profile" --no-first-run --no-default-browser-check "$@"
