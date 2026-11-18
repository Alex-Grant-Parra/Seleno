"""Just enough of Cloudflare's DNS API for telescopes' offline names
(app/offline_access.py): an A record per telescope pointing at its hotspot
address, and the TXT records Let's Encrypt's DNS-01 challenge asks for.

Needs CLOUDFLARE_API_TOKEN (a token with Zone:DNS:Edit on the zone) and the
zone's name, CLOUDFLARE_ZONE (default: APP_DOMAIN). Records are DNS only, never
proxied: a phone must reach the address itself."""
import logging
import os

import requests

logger = logging.getLogger(__name__)

API = "https://api.cloudflare.com/client/v4"
TIMEOUT_S = 20


class DnsError(RuntimeError):
    pass


def configured():
    return bool(os.getenv("CLOUDFLARE_API_TOKEN"))


def zone_name():
    return (os.getenv("CLOUDFLARE_ZONE") or os.getenv("APP_DOMAIN") or "seleno.org").strip().lower()


class Cloudflare:
    def __init__(self, token=None, zone=None, session=None):
        self.token = token or os.getenv("CLOUDFLARE_API_TOKEN") or ""
        self.zone = zone or zone_name()
        self.session = session or requests.Session()
        self._zone_id = None
        if not self.token:
            raise DnsError("CLOUDFLARE_API_TOKEN isn't set")

    def _call(self, method, path, **kwargs):
        resp = self.session.request(method, API + path, timeout=TIMEOUT_S,
                                    headers={"Authorization": f"Bearer {self.token}"}, **kwargs)
        try:
            data = resp.json()
        except ValueError:
            raise DnsError(f"Cloudflare answered {resp.status_code}")
        if not data.get("success"):
            errors = "; ".join(str(e.get("message")) for e in data.get("errors") or []) or str(resp.status_code)
            raise DnsError(f"Cloudflare: {errors}")
        return data.get("result")

    def zone_id(self):
        if self._zone_id is None:
            zones = self._call("GET", "/zones", params={"name": self.zone})
            if not zones:
                raise DnsError(f"No Cloudflare zone {self.zone}")
            self._zone_id = zones[0]["id"]
        return self._zone_id

    def records(self, name, kind):
        return self._call("GET", f"/zones/{self.zone_id()}/dns_records", params={"name": name, "type": kind}) or []

    def set_a(self, name, address):
        """name -> address, DNS only; one record, made or changed."""
        body = {"type": "A", "name": name, "content": address, "ttl": 300, "proxied": False}
        existing = self.records(name, "A")
        if existing:
            record = existing[0]
            if record.get("content") == address and not record.get("proxied"):
                return record["id"]
            return self._call("PUT", f"/zones/{self.zone_id()}/dns_records/{record['id']}", json=body)["id"]
        return self._call("POST", f"/zones/{self.zone_id()}/dns_records", json=body)["id"]

    def add_txt(self, name, value):
        body = {"type": "TXT", "name": name, "content": value, "ttl": 60}
        return self._call("POST", f"/zones/{self.zone_id()}/dns_records", json=body)["id"]

    def delete(self, record_id):
        try:
            self._call("DELETE", f"/zones/{self.zone_id()}/dns_records/{record_id}")
        except DnsError as exc:
            logger.warning("Couldn't remove DNS record %s: %s", record_id, exc)
