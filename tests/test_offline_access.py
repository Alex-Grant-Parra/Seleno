"""The app talking straight to a telescope offline (app/offline_access.py):
telescopes' names, users' offline passes, the credentials telescopes fetch,
the offline pack, and certificates from a fake ACME server by DNS-01."""
import base64
import datetime
import hashlib
import hmac
import json
import os
import time
import unittest
from unittest import mock

from harness import app, client, db, login, make_booking, make_telescope, make_user

from app import acme, offline_access
from models.telescope_offline import TelescopeOffline
from models.telescope_share import TelescopeShare


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_pass(secret, token, telescope_id):
    """The telescope's check (SelenoClient core/networking/phone_link.verify)."""
    prefix, body, mac = token.split(".")
    expected = hmac.new(secret.encode(), f"{prefix}.{body}".encode(), hashlib.sha256).digest()
    if prefix != "v1" or not hmac.compare_digest(expected, unb64(mac)):
        return None
    claims = json.loads(unb64(body))
    if claims["t"] != telescope_id or claims["exp"] < time.time():
        return None
    return claims


class Passes(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        suffix = str(time.time_ns())[-8:]
        self.owner = make_user(f"oa-owner-{suffix}")
        self.friend = make_user(f"oa-friend-{suffix}")
        self.booker = make_user(f"oa-booker-{suffix}")
        self.scope = make_telescope(f"Garden Dob {suffix}", owner=self.owner, token=f"oa-token-{suffix}-0123456789")
        self.token = f"oa-token-{suffix}-0123456789"
        db.session.add(TelescopeShare(telescope_id=self.scope.id, user_id=self.friend.id, granted_by_user_id=self.owner.id))
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def test_names(self):
        record = offline_access.record_for(self.scope)
        self.assertRegex(record.slug, r"^garden-dob-\d+-\d+$")
        self.assertEqual(offline_access.host_for(record), f"{record.slug}.tel.seleno.org")
        self.assertIs(offline_access.record_for(self.scope).slug, record.slug)

    def test_owner_shared_and_booking(self):
        record = offline_access.record_for(self.scope)
        mine = offline_access.passes_for(self.owner)
        self.assertEqual([(p["telescope_id"], p["role"]) for p in mine], [(self.scope.telescope_id, "owner")])
        claims = verify_pass(record.secret, mine[0]["pass"], self.scope.telescope_id)
        self.assertEqual((claims["u"], claims["r"]), (self.owner.id, "owner"))
        self.assertEqual(mine[0]["host"], offline_access.host_for(record))
        self.assertEqual(mine[0]["api_port"], 8443)
        self.assertEqual([p["role"] for p in offline_access.passes_for(self.friend)], ["shared"])
        self.assertEqual(offline_access.passes_for(self.booker), [])
        start = datetime.datetime.utcnow() + datetime.timedelta(days=2)
        make_booking(self.scope, self.booker, start, start + datetime.timedelta(hours=3), status="reserved")
        booked = offline_access.passes_for(self.booker)
        self.assertEqual([p["role"] for p in booked], ["booking"])
        from calendar import timegm
        self.assertEqual(booked[0]["nbf"], timegm(start.timetuple()))
        self.assertEqual(booked[0]["exp"] - booked[0]["nbf"], 3 * 3600)

    def test_passes_endpoint(self):
        c = app.test_client()
        self.assertEqual(c.get("/interface/offline_passes").status_code, 401)
        login(c, self.owner)
        data = c.get("/interface/offline_passes").get_json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["telescopes"][0]["telescope_id"], self.scope.telescope_id)

    def test_credentials_for_the_telescope(self):
        r = client.get("/api/telescope/offline_credentials")
        self.assertEqual(r.status_code, 401)
        r = client.get("/api/telescope/offline_credentials", headers={"Authorization": "Bearer wrong-token-0123456789"})
        self.assertEqual(r.status_code, 401)
        with mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": ""}):
            data = client.get("/api/telescope/offline_credentials",
                              headers={"Authorization": f"Bearer {self.token}"}).get_json()
        record = offline_access.record_for(self.scope)
        self.assertEqual(data["telescope_id"], self.scope.telescope_id)
        self.assertEqual(data["secret"], record.secret)
        self.assertEqual(data["host"], offline_access.host_for(record))
        self.assertEqual(data["address"], "10.42.0.1")
        self.assertIn("https://seleno.org", data["origins"])
        self.assertIsNone(data["cert"])
        self.assertEqual(data["certificate"], "not set up on the server")
        self.assertIn("featured_targets", data["pack"])
        self.assertIn("objects", data["pack"])

    def test_the_secret_is_stored_encrypted(self):
        record = offline_access.record_for(self.scope)
        raw = db.session.execute(db.text("SELECT secret FROM telescope_offline WHERE telescope_row_id = :id"),
                                 {"id": self.scope.id}).scalar()
        self.assertNotEqual(raw, record.secret)

    def test_job_does_nothing_without_cloudflare(self):
        with mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": ""}):
            self.assertEqual(offline_access.run_job(app), 0)


# --- certificates --------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, data=None, headers=None, text=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(data)

    def json(self):
        return self._data


class FakeAcme:
    """An ACME server that checks every JWS and the DNS-01 record."""

    BASE = "https://acme.test"

    def __init__(self, dns):
        self.dns = dns
        self.account_jwk = None
        self.nonce = 0
        self.authz_status = "pending"
        self.order_status = "pending"
        self.csr = None
        self.token = "tok-" + "x" * 20

    def _nonce(self):
        self.nonce += 1
        return f"n{self.nonce}"

    def get(self, url, timeout=None):
        return FakeResponse(data={k: f"{self.BASE}/{k}" for k in ("newNonce", "newAccount", "newOrder")})

    def head(self, url, timeout=None):
        return FakeResponse(headers={"Replay-Nonce": self._nonce()})

    def _check(self, url, body):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        protected = json.loads(unb64(body["protected"]))
        assert protected["url"] == url and protected["alg"] == "ES256"
        if "jwk" in protected:
            self.account_jwk = protected["jwk"]
        else:
            assert protected["kid"] == f"{self.BASE}/acct/1"
        numbers = ec.EllipticCurvePublicNumbers(int.from_bytes(unb64(self.account_jwk["x"]), "big"),
                                                int.from_bytes(unb64(self.account_jwk["y"]), "big"), ec.SECP256R1())
        sig = unb64(body["signature"])
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        numbers.public_key().verify(der, f"{body['protected']}.{body['payload']}".encode(), ec.ECDSA(hashes.SHA256()))
        return json.loads(unb64(body["payload"])) if body["payload"] else None

    def post(self, url, data=None, timeout=None, headers=None):
        payload = self._check(url, json.loads(data))
        h = {"Replay-Nonce": self._nonce()}
        if url.endswith("/newAccount"):
            return FakeResponse(201, {"status": "valid"}, dict(h, Location=f"{self.BASE}/acct/1"))
        if url.endswith("/newOrder"):
            self.name = payload["identifiers"][0]["value"]
            return FakeResponse(201, {"status": "pending", "authorizations": [f"{self.BASE}/authz/1"],
                                      "finalize": f"{self.BASE}/finalize"}, dict(h, Location=f"{self.BASE}/order/1"))
        if url.endswith("/authz/1"):
            return FakeResponse(200, {"status": self.authz_status, "identifier": {"type": "dns", "value": self.name},
                                      "challenges": [{"type": "http-01", "url": "x", "token": "y"},
                                                     {"type": "dns-01", "url": f"{self.BASE}/chall/1", "token": self.token}]}, h)
        if url.endswith("/chall/1"):
            from cryptography.hazmat.primitives.asymmetric import ec as _ec  # noqa: F401
            jwk_json = json.dumps(self.account_jwk, sort_keys=True, separators=(",", ":")).encode()
            thumb = acme.b64(hashlib.sha256(jwk_json).digest())
            expected = acme.b64(hashlib.sha256(f"{self.token}.{thumb}".encode()).digest())
            assert self.dns.txt.get(f"_acme-challenge.{self.name}") == expected, self.dns.txt
            self.authz_status = "valid"
            return FakeResponse(200, {"status": "processing"}, h)
        if url.endswith("/finalize"):
            from cryptography import x509
            self.csr = x509.load_der_x509_csr(unb64(payload["csr"]))
            self.order_status = "valid"
            return FakeResponse(200, {"status": "processing"}, h)
        if url.endswith("/order/1"):
            return FakeResponse(200, {"status": self.order_status, "certificate": f"{self.BASE}/cert/1"}, h)
        if url.endswith("/cert/1"):
            return FakeResponse(200, None, h, text=self._certificate())
        return FakeResponse(404, {"detail": "no"}, h)

    def _certificate(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        issuer_key = acme.new_key()
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(self.csr.subject).issuer_name(self.csr.subject)
                .public_key(self.csr.public_key()).serial_number(1).not_valid_before(now)
                .not_valid_after(now + datetime.timedelta(days=90)).sign(issuer_key, hashes.SHA256()))
        return cert.public_bytes(serialization.Encoding.PEM).decode()


class FakeDns:
    def __init__(self):
        self.txt, self.a, self.deleted = {}, {}, []

    def add_txt(self, name, value):
        self.txt[name] = value
        return name

    def delete(self, record_id):
        self.deleted.append(record_id)
        self.txt.pop(record_id, None)

    def set_a(self, name, address):
        self.a[name] = address


class Certificates(unittest.TestCase):
    def test_issue_and_store(self):
        with app.app_context():
            owner = make_user(f"oa-cert-{time.time_ns()}")
            scope = make_telescope(f"Cert scope {time.time_ns()}", owner=owner)
            record = offline_access.record_for(scope)
            dns = FakeDns()
            server = FakeAcme(dns)
            client_ = acme.Client(acme.new_key(), directory=f"{FakeAcme.BASE}/directory", session=server,
                                  sleep=lambda s: None)
            self.assertTrue(offline_access.ensure(record, dns, client_))
            host = offline_access.host_for(record)
            self.assertEqual(dns.a, {host: "10.42.0.1"})
            self.assertEqual(dns.deleted, [f"_acme-challenge.{host}"])  # the challenge record is removed
            fresh = db.session.get(TelescopeOffline, scope.id)
            self.assertIn("BEGIN CERTIFICATE", fresh.cert_pem)
            self.assertIn("PRIVATE KEY", fresh.key_pem)
            self.assertGreater(fresh.cert_expires, datetime.datetime.utcnow() + datetime.timedelta(days=80))
            self.assertEqual([n.value for n in server.csr.subject], [host])
            # Not due again for a while
            self.assertFalse(offline_access.ensure(fresh, dns, client_))


if __name__ == "__main__":
    unittest.main()
