# ASTRA Client Quick Setup

Use this guide when you receive the client as a zip file containing:

- `config/`
- `core/`
- `esp32/`
- `utils/`
- `Client.py`
- `requirements.txt`
- `README.md`

## Runtime requirements

- Linux (recommended: Ubuntu 2.4+)
- Python 3.10+

## 1. Extract and open the project

1. Unzip the package.
2. Open a terminal in the extracted folder (the folder that contains `Client.py`).

## 2. Install Linux system packages

The client code directly calls `gphoto2` and `pkill`, and uses serial USB devices (`/dev/ttyUSB*`, `/dev/ttyACM*`).

Debian/Ubuntu/Raspberry Pi OS:

    sudo apt update
    sudo apt install -y gphoto2 libgphoto2-dev procps

Optional (only if you want hardware GPS via `gpsd`):

    sudo apt install -y gpsd gpsd-clients python3-gps

## 3. USB permissions (recommended)

Give your user access to serial and removable-device groups, then re-login:

    sudo usermod -aG dialout,plugdev $USER

After running this command, log out and log back in (or reboot).

## 4. Create and activate a virtual environment

Linux/macOS:

    python3 -m venv venv
    source venv/bin/activate

Windows (PowerShell):

    py -m venv venv
    .\venv\Scripts\Activate.ps1

## 5. Install dependencies

    pip install -r requirements.txt

## 6. Configure the client profile

Edit `config/client_profile.json` and set the required values:

- `client_config.client_id` 
- `client_config.base_url` (example: `https://telescopes.dev/`)
- `client_config.api_token`

With the SELENO front panel you can enter these on the panel at first start
instead, or choose **Offline** to use the telescope without a server
(`client_config.offline: true`): go-to and photos from the panel, with photos
saved in `photos/offline/`.

## 7. Start the client

    python Client.py

Optional setup mode:

    python Client.py setup

## Quick check if something fails

- Confirm your terminal is in the project root (same folder as `Client.py`).
- Confirm your virtual environment is active.
- Confirm `client_profile.json` has valid non-empty values.
- Confirm `gphoto2 --auto-detect` can see your camera.
- Confirm your ESP32 serial device exists (`ls /dev/ttyUSB* /dev/ttyACM*`).
- Confirm your user is in `dialout` (and usually `plugdev`) groups.
- Re-run dependency install:

    pip install -r requirements.txt
