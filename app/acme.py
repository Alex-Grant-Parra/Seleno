"""A small ACME (RFC 8555) client: a certificate for one name, by DNS-01.

For telescopes' offline names (app/offline_access.py). Their names point at a
private address no one can reach from the internet, so the HTTP challenge is
out; DNS-01 proves control of the name with a TXT record instead, written
through app/cloudflare_dns.py.

    issue(name, dns, account_key) -> (fullchain PEM, key PEM, not_after)

The account key is P-256 (ES256); each certificate gets its own P-256 key.
ACME_DIRECTORY picks the CA (default Let's Encrypt; its staging directory
for trying things out)."""
import base64
import hashlib
import json
import logging
import os
import time

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

LETS_ENCRYPT = "https://acme-v02.api.letsencrypt.org/directory"
LETS_ENCRYPT_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
POLL_S = 2.0
POLL_TRIES = 60
DNS_WAIT_S = 15.0  # for the TXT record to reach the zone's servers


class AcmeError(RuntimeError):
    pass


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def directory_url():
    return os.getenv("ACME_DIRECTORY") or LETS_ENCRYPT


def new_key():
    return ec.generate_private_key(ec.SECP256R1())


def key_pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def load_key(pem):
    return serialization.load_pem_private_key(pem.encode() if isinstance(pem, str) else pem, password=None)


def jwk(key):
    numbers = key.public_key().public_numbers()
    return {"crv": "P-256", "kty": "EC", "x": b64(numbers.x.to_bytes(32, "big")), "y": b64(numbers.y.to_bytes(32, "big"))}


def thumbprint(key):
    return b64(hashlib.sha256(json.dumps(jwk(key), sort_keys=True, separators=(",", ":")).encode()).digest())


def dns_txt_value(token, key):
    """The TXT record's value for a DNS-01 challenge's token."""
    return b64(hashlib.sha256(f"{token}.{thumbprint(key)}".encode()).digest())


class Client:
    def __init__(self, account_key, directory=None, session=None, sleep=time.sleep):
        self.key = account_key
        self.session = session or requests.Session()
        self.sleep = sleep
        self.directory = self.session.get(directory or directory_url(), timeout=30).json()
        self.kid = None
        self.nonce = None

    def _nonce(self):
        if self.nonce:
            nonce, self.nonce = self.nonce, None
            return nonce
        resp = self.session.head(self.directory["newNonce"], timeout=30)
        return resp.headers["Replay-Nonce"]

    def _sign(self, url, payload):
        protected = {"alg": "ES256", "nonce": self._nonce(), "url": url}
        if self.kid:
            protected["kid"] = self.kid
        else:
            protected["jwk"] = jwk(self.key)
        protected64 = b64(json.dumps(protected).encode())
        payload64 = "" if payload is None else b64(json.dumps(payload).encode())
        der = self.key.sign(f"{protected64}.{payload64}".encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return {"protected": protected64, "payload": payload64,
                "signature": b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}

    def post(self, url, payload=None, retry=True):
        """A signed POST (payload None: POST-as-GET)."""
        resp = self.session.post(url, data=json.dumps(self._sign(url, payload)), timeout=30,
                                 headers={"Content-Type": "application/jose+json"})
        self.nonce = resp.headers.get("Replay-Nonce")
        if resp.status_code >= 400:
            try:
                problem = resp.json()
            except ValueError:
                problem = {"detail": resp.text[:200]}
            if retry and problem.get("type", "").endswith(":badNonce"):
                return self.post(url, payload, retry=False)
            raise AcmeError(f"{url}: {resp.status_code} {problem.get('detail') or problem}")
        return resp

    def register(self, email=None):
        payload = {"termsOfServiceAgreed": True}
        if email:
            payload["contact"] = [f"mailto:{email}"]
        resp = self.post(self.directory["newAccount"], payload)
        self.kid = resp.headers["Location"]
        return self.kid

    def _poll(self, url, done=("valid",), failed=("invalid",)):
        for _ in range(POLL_TRIES):
            data = self.post(url).json()
            if data.get("status") in done:
                return data
            if data.get("status") in failed:
                raise AcmeError(f"{url}: {data.get('status')}: {json.dumps(data.get('error') or data)[:300]}")
            self.sleep(POLL_S)
        raise AcmeError(f"{url}: still {data.get('status')}")

    def issue(self, name, dns):
        """A certificate for name: DNS-01 through dns (add_txt/delete)."""
        if not self.kid:
            self.register(os.getenv("ACME_EMAIL"))
        order_resp = self.post(self.directory["newOrder"], {"identifiers": [{"type": "dns", "value": name}]})
        order_url = order_resp.headers["Location"]
        order = order_resp.json()
        records = []
        try:
            challenges = []
            for authz_url in order["authorizations"]:
                authz = self.post(authz_url).json()
                if authz.get("status") == "valid":
                    continue
                challenge = next((c for c in authz["challenges"] if c["type"] == "dns-01"), None)
                if challenge is None:
                    raise AcmeError("The CA offers no DNS-01 challenge")
                records.append(dns.add_txt(f"_acme-challenge.{authz['identifier']['value']}",
                                           dns_txt_value(challenge["token"], self.key)))
                challenges.append((authz_url, challenge["url"]))
            if challenges:
                self.sleep(DNS_WAIT_S)
            for authz_url, challenge_url in challenges:
                self.post(challenge_url, {})
                self._poll(authz_url)
            cert_key = new_key()
            csr = (x509.CertificateSigningRequestBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
                   .sign(cert_key, hashes.SHA256()))
            self.post(order["finalize"], {"csr": b64(csr.public_bytes(serialization.Encoding.DER))})
            order = self._poll(order_url, failed=("invalid",))
            chain = self.post(order["certificate"]).text
        finally:
            for record in records:
                dns.delete(record)
        not_after = x509.load_pem_x509_certificate(chain.encode()).not_valid_after_utc.replace(tzinfo=None)
        return chain, key_pem(cert_key), not_after
