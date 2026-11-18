# SELENO - Automated Sidereal Tracking and Remote Astronomy

## A modern telescope automation system for remote observation, object tracking and astrophotography with a web-based observatory management platform.

---

## Features

* Web-based telescope control  
* Distributed telescope network management  
* Automated target acquisition and tracking  
* Astrophotography image capture  
* Observation scheduling and automation  
* Interactive planetarium interface  
* Astronomical object database  
* Solar system object tracking  
* Remote camera and mount control  
* REST API for astronomical data access  
* Raspberry Pi and ESP32 powered telescope nodes  
* Beginner-friendly operation with advanced manual controls  
* Source-available and self-hostable

## System Architecture

SELENO uses a web-based control server connected to distributed Raspberry Pi \+ ESP32 telescope nodes. Commands, live view camera frames, and tracking data flow through WebSocket channels, while each node handles its own motor control, sensors, and imaging pipeline.

![System Architecture](https://raw.githubusercontent.com/Alex-Grant-Parra/SELENO/Server/canary/docs/System%20Architecture.drawio.png)

## Client Setup (For running SELENO on your telescope system)

* In the email you were sent when you purchased the telescope system, select register. This will give you a client ID, API token and server URL.  
* Create an account, and link your client ID and API key to it, following the on screen directions.  
* Power on the system, and wait for the blue LED to stay constantly lit.  
* Use the keypad to enter in the client ID, API token and server URL. There will be an on screen confirmation when connected.  
* (Recommended) Check for updates in the settings window. This may take several minutes if updates are found. Do not power off your device.   
* Log in and access the main interface page. It is recommended that you follow the first time tutorial on how to use the site.

## ADVANCED - Server Setup (For running your own control server)

 * Follow this guide to learn how to do this:
 https://github.com/Alex-Grant-Parra/SELENO/wiki/Server-Setup-(For-running-your-own-control-server)

 ## Core Runtime Behavior

- Flask app boots from Server.py and is served by waitress on 127.0.0.1:5000.
- Blueprints are auto-loaded from controllers/.
- SQLite database is stored at instance/Data.db.
- WebSocket services start with the app:
	- Command server: port 4000
	- LiveView server: port 8000
- Route list is auto-generated to templates/routes.txt at startup.

## Offline mode (on a telescope's Pi)

A telescope in offline mode makes its own Wi-Fi hotspot, with no internet, and runs a copy of this
server on its Raspberry Pi for the phones that join it (SelenoClient's `core/networking/hotspot.py`
and `offline_server.py`). Phones open `http://10.42.0.1/` and get the interface, star map, album and
Telescope Settings as online, with just that telescope (`app/offline_mode.py`):

- one account, the observer, which every visitor is (the Wi-Fi password is the way in), and one
  telescope, owned by it and selected for everyone, named by the telescope;
- the other pages redirect to the interface; the CDN files are served locally;
- live view goes straight from the Pi to the phone (`ws://<address the phone used>:8000`).

The copy is a bundle built here and downloaded by telescopes with their API token from
`/api/telescope/offline_bundle` (the panel's Setup > Get phone app). Rebuild it after changing the
pages (about 15 s; needs the internet once, for the CDN files):

```bash
venv/bin/python scripts/build_offline_bundle.py    # -> downloads/offline/seleno-offline.tar.gz
```

It holds the code, the star catalogue (no accounts, logs or photos), V3 Helios's checkpoint cache
(not its SPICE kernels) and local copies of Bootstrap, Font Awesome and the fonts.

Telescope Settings (`/interface/telescope_settings/`, `controllers/telescope_settings.py`) works online
too, for a telescope's owner: it shows and changes the telescope's own panel settings through the
client's `getSettings` and `setSettings`.

## Important Environment Variables

- FLASK_SECRET_KEY: Flask session secret (required: the server won't start without it)
- ENCRYPTION_KEY: application encryption key
- FLASK_BIND_HOST: address the web server binds to (default 127.0.0.1; the Cloudflare tunnel is the way in)
- FLASK_PORT: the web server's port (default 5000)
- SELENO_OFFLINE, SELENO_OFFLINE_TELESCOPE, SELENO_OFFLINE_TOKEN: offline mode on a telescope's Pi, its telescope's name and token (set by the telescope client, see above)
- LIVEVIEW_WS_IP: the live-view socket's bind address, when it differs from WS_IP (offline mode: phones connect to it directly)
- WS_IP: WebSocket bind address (default 127.0.0.1)
- TRUSTED_PROXIES: extra proxy addresses whose forwarding headers are believed (loopback always is)
- FLASK_DEBUG: run Flask's development server with the reloader instead of waitress
- WAITRESS_THREADS, WAITRESS_CHANNEL_TIMEOUT: waitress worker threads (default 48) and idle timeout in s (default 300)
- COOKIE_SECURE: send session cookies over HTTPS only (default True; False for plain-HTTP development)
- LOCAL_AUTH_BYPASS: let visitors on this machine or its private network skip emailed 2FA and see reset links on the page (default False)
- LOG_RETENTION_DAYS: delete request and security log rows older than this, daily (default unset: keep everything)
- AUTO_INSTALL_REQUIREMENTS: pip-install missing requirements at startup (default False: only warn); set it in the environment, not instance/.env, which is read after the check
- APP_DOMAIN: the site's domain, used for HTTPS redirects
- MAX_UPLOAD_BYTES: max upload size (default 128 MiB)
- WS_PING_INTERVAL, WS_PING_TIMEOUT: WebSocket keepalive settings
- TURNSTILE_SITE_KEY, TURNSTILE_SECRET_KEY: CAPTCHA support
- SUMUP_MODE: `sandbox` (default) or `live`. In sandbox only admins can check out at /store and orders are marked as tests
- SUMUP_TEST_API_KEY, SUMUP_TEST_MERCHANT_CODE / SUMUP_API_KEY, SUMUP_MERCHANT_CODE: SumUp keys for each mode
- SUMUP_WEBHOOK_SECRET: optional token SumUp's payment notices must carry
- STORE_NOTIFY_EMAIL: where new paid orders are announced (default: the support sender)

Install dependencies with `venv/bin/pip install -r utility/requirements.txt` (pinned versions).

## Planetary Models

SELENO includes a few different planetary models under the astrophysics folder, each with varying precision.
- V1_Keplarian is a traditional keplarian algorithm
- V2_VSOP87A is an iterative solution, taking the sum of a large number of orbital parameters
- V3_Helios is a custom N-body engine, with a local integrator and engine, with initial parameters provided by JPL SPICE data

## Notes

- instance/.env contains secrets and should not be committed.
- infrastructure/ contains deployment-related files and logs.
- When downloading the server, the database will not be included. This may change in future versions, but you will have to supply your own data for now.
