"""The address a request really came from.

Behind the Cloudflare tunnel (or a reverse proxy on this machine) every
request reaches Flask from loopback, and the proxy names the visitor in a
header. Those headers are believed only when the connection itself came from
a proxy we trust: loopback, or an address listed in TRUSTED_PROXIES. Sent
straight to the port, they are whatever the sender typed.

X-Forwarded-For is read from the right. Each proxy appends the address it
saw, so the last entry is the one our own proxy added; the first is whatever
the visitor sent, and "127.0.0.1" there proves nothing.
"""
import ipaddress
import os

from flask import request


def parse_ip(value):
    """An ip_address for the text, or None. IPv4-mapped IPv6 becomes IPv4."""
    try:
        ip = ipaddress.ip_address((value or '').strip())
    except ValueError:
        return None
    mapped = getattr(ip, 'ipv4_mapped', None)
    return mapped or ip


def is_trusted_proxy(address) -> bool:
    """True when forwarding headers from this peer can be believed."""
    ip = parse_ip(address)
    if ip is None:
        return False
    if ip.is_loopback:
        return True
    extra = {parse_ip(part) for part in os.getenv('TRUSTED_PROXIES', '').split(',')}
    return ip in extra


def forwarded_ip(headers):
    """The visitor's address as a trusted proxy reported it, or None."""
    for name in ('CF-Connecting-IP', 'True-Client-IP'):
        ip = parse_ip(headers.get(name))
        if ip is not None:
            return str(ip)
    chain = headers.get('X-Forwarded-For')
    if chain:
        ip = parse_ip(chain.split(',')[-1])
        if ip is not None:
            return str(ip)
    ip = parse_ip(headers.get('X-Real-IP'))
    return str(ip) if ip is not None else None


def client_ip() -> str:
    """The visitor's address for the current Flask request."""
    remote = request.remote_addr or ''
    if is_trusted_proxy(remote):
        reported = forwarded_ip(request.headers)
        if reported:
            return reported
    return remote or 'unknown'


def is_local_request() -> bool:
    """True when the visitor is on this machine or its private network."""
    text = client_ip()
    ip = parse_ip(text)
    if ip is None:
        return False
    if ip.is_loopback or ip.is_private:
        return True
    # LOCAL_IP_ADDRESSES may list addresses or prefixes such as "192.168.0"
    for entry in os.getenv('LOCAL_IP_ADDRESSES', '').split(','):
        entry = entry.strip().rstrip('.')
        if entry and (text == entry or text.startswith(entry + '.')):
            return True
    return False
