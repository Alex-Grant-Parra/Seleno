# SELENO Client Quick Setup

Use this guide when you receive the client as a zip file containing:

- `config/`
- `core/`
- `esp32/`
- `utils/`
- `Client.py`
- `requirements.txt`
- `README.md`

## Runtime requirements

- Linux on a Raspberry Pi 5 (Ubuntu or Raspberry Pi OS)
- Python 3.10+ (3.12 tested)

## 1. Extract and open the project

1. Unzip the package.
2. Open a terminal in the extracted folder (the folder that contains `Client.py`).

## 2. Install Linux system packages

The client talks to the camera through libgphoto2 (the `gphoto2` Python
package in requirements.txt brings its own copy). The `gphoto2` command is
only needed as a fallback. Serial USB devices are `/dev/ttyUSB*` and
`/dev/ttyACM*`.

Debian/Ubuntu/Raspberry Pi OS:

    sudo apt update
    sudo apt install -y gphoto2

Optional, for a GPS receiver (the client reads gpsd itself; no Python
package is needed):

    sudo apt install -y gpsd

Optional, for plate solving: ASTAP's command-line solver and a star
database, unpacked into `~/astap`:
- `astap_command-line_version_Linux_aarch64.zip`;
- `d05_star_database.zip` for telescope fields, `w08_star_database_mag08_astap.zip`
  for wide lenses.

Both come from the ASTAP project on SourceForge.

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
- `client_config.base_url` (your ASTRA server, for example `seleno.org`)
- `client_config.api_token`

With the SELENO front panel you can enter these on the panel at first start
instead, or choose **Offline** to use the telescope without a server
(`client_config.offline: true`): go-to and photos from the panel, with photos
saved in `photos/offline/`.

## 7. Start the client

    python Client.py

Optional setup mode:

    python Client.py setup

To start it with the Pi, install the systemd unit (instructions at the top
of `deploy/seleno-client.service`):

    sudo cp deploy/seleno-client.service /etc/systemd/system/
    sudo systemctl enable --now seleno-client.service

The log is in the journal (`journalctl -u seleno-client -f`) and in
`logs/seleno.log`.

## Quick check if something fails

- Confirm your terminal is in the project root (same folder as `Client.py`).
- Confirm your virtual environment is active.
- Confirm `client_profile.json` has valid non-empty values.
- Confirm `gphoto2 --auto-detect` can see your camera (stop the client first: it holds the camera).
- No camera to hand? Set `"camera": {"backend": "fake"}` in `config/client_config.json`.
- Confirm your ESP32 serial device exists (`ls /dev/ttyUSB* /dev/ttyACM*`).
- Confirm your user is in `dialout` (and usually `plugdev`) groups.
- Re-run dependency install:

    pip install -r requirements.txt
