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

## A telescope's own Wi-Fi (offline)

A telescope in offline mode makes its own Wi-Fi hotspot, with no internet (SelenoClient's
`core/networking/hotspot.py`). Phones on it keep using the seleno.org app, installed from here:

- **The app works offline.** Its service worker (`static/js/star_map_sw.js`) keeps the interface,
  star map, album and Telescope Settings pages, the site's static files and the CDN files, so the
  app opens with no internet. The star map runs on the phone as always.
- **It talks straight to the telescope.** `static/js/offline-link.js` finds a telescope of the
  user's nearby and tells the worker, which from then on sends the requests that are about the
  telescope (the interface's commands and status, tracking, the album's files...) to the telescope
  itself; the telescope answers them as this server would (SelenoClient `utils/phone_api.py`). The
  interface's telescope list then has just that telescope. It looks by itself when seleno.org can't
  be reached (the telescope's Wi-Fi has no internet), or after the browser has allowed local
  network access.
- **HTTPS at the telescope's own name.** A page from https://seleno.org can only talk https, so
  each telescope gets `<slug>.tel.<APP_DOMAIN>`, pointing at its hotspot address (10.42.0.1), with a
  Let's Encrypt certificate this server gets by DNS-01 on Cloudflare and renews (`app/acme.py`,
  `app/cloudflare_dns.py`, `app/offline_access.py`). Telescopes fetch their name, certificate and
  pass secret with their API token from `/api/telescope/offline_credentials` while online.
- **Offline passes.** While online the app gets a signed pass for each telescope the user may use
  (`/interface/offline_passes`: owner and shared for 30 days, bookings for the booking's time), and
  the telescope checks it with the secret they share.

Needs `CLOUDFLARE_API_TOKEN` (a token with Zone:DNS:Edit on the zone) for the names and
certificates; without it everything else works and telescopes are told their certificate is
pending. Capture plans, stacking and saving edits stay online-only.

Telescope Settings (`/interface/telescope_settings/`, `controllers/telescope_settings.py`) shows and
changes a telescope's own panel settings for its owner, online or on the hotspot.

The whole flow runs end to end in headless Chrome against a fake telescope:
`E2E_CHROME=/path/to/chrome venv/bin/python tests/e2e/hotspot.py` (about 15 s).

## Important Environment Variables

- FLASK_SECRET_KEY: Flask session secret (required: the server won't start without it)
- ENCRYPTION_KEY: application encryption key
- FLASK_BIND_HOST: address the web server binds to (default 127.0.0.1; the Cloudflare tunnel is the way in)
- WS_IP: WebSocket bind address (default 127.0.0.1)
- CLOUDFLARE_API_TOKEN, CLOUDFLARE_ZONE: telescopes' offline names and certificates (see above; zone default APP_DOMAIN)
- OFFLINE_TLS_SUFFIX (default tel.<APP_DOMAIN>), OFFLINE_HOTSPOT_ADDRESS (default 10.42.0.1), OFFLINE_APP_ORIGINS (extra origins telescopes accept), ACME_DIRECTORY (default Let's Encrypt; its staging directory for trying), ACME_EMAIL
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
