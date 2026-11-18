#!/bin/sh
# Set the Pi up for the offline hotspot (core/networking/hotspot.py). Once, with sudo:
#
#   sudo deploy/hotspot-setup.sh            # for the user running the client (sudo's user)
#   sudo deploy/hotspot-setup.sh alex       # or name them
#
# It needs the internet (it installs hostapd). It:
#   * installs hostapd (the hotspot itself), stopped and not started at boot,
#     and dnsmasq-base (answers the telescope's own name on the hotspot, for
#     the seleno.org app; no system DNS service comes with it);
#   * installs deploy/seleno-hotspot as /usr/local/sbin/seleno-hotspot, and
#     seleno-hostapd.service, which runs hostapd with the settings it writes;
#   * lets that user run seleno-hotspot (only that) through sudo without a
#     password;
#   * lets ordinary programs use port 80, so phones open http://10.42.0.1/
#     rather than :5000 (net.ipv4.ip_unprivileged_port_start=80).
#
# Nothing changes on the Wi-Fi until the client turns the hotspot on (offline
# mode, Setup > Phone hotspot). Undo: sudo deploy/hotspot-setup.sh --remove
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
HELPER=/usr/local/sbin/seleno-hotspot
UNIT=/etc/systemd/system/seleno-hostapd.service
DNS_UNIT=/etc/systemd/system/seleno-dns.service
SUDOERS=/etc/sudoers.d/seleno-hotspot
SYSCTL=/etc/sysctl.d/60-seleno-hotspot.conf

if [ "$(id -u)" -ne 0 ]; then
    echo "Run it with sudo: sudo $0 $*" >&2
    exit 1
fi

if [ "${1:-}" = "--remove" ]; then
    "$HELPER" down </dev/null >/dev/null 2>&1 || true
    rm -f "$HELPER" "$UNIT" "$DNS_UNIT" "$SUDOERS" "$SYSCTL"
    systemctl daemon-reload
    sysctl -q -w net.ipv4.ip_unprivileged_port_start=1024 || true
    echo "Removed. (hostapd is still installed: sudo apt remove hostapd)"
    exit 0
fi

USER_NAME=${1:-${SUDO_USER:-}}
if [ -z "$USER_NAME" ] || ! id "$USER_NAME" >/dev/null 2>&1; then
    echo "Which user runs the client? sudo $0 <user>" >&2
    exit 1
fi

if ! command -v hostapd >/dev/null 2>&1 || [ ! -x /usr/sbin/dnsmasq ]; then
    echo "Installing hostapd and dnsmasq-base..."
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q hostapd dnsmasq-base
fi
# Only seleno-hostapd.service runs it, when the client asks
systemctl disable --now hostapd.service >/dev/null 2>&1 || true

install -m 0755 -o root -g root "$HERE/seleno-hotspot" "$HELPER"

cat > "$UNIT" <<'EOF'
# The SELENO offline hotspot: started and stopped by /usr/local/sbin/seleno-hotspot
[Unit]
Description=SELENO offline Wi-Fi hotspot
After=network-pre.target

[Service]
Type=simple
ExecStart=/usr/sbin/hostapd /run/seleno-hotspot/hostapd.conf
Restart=on-failure
RestartSec=3
EOF
cat > "$DNS_UNIT" <<'UNIT'
# The SELENO offline hotspot's DNS: just the telescope's own name, for the
# seleno.org app. Started and stopped by /usr/local/sbin/seleno-hotspot
[Unit]
Description=SELENO offline hotspot DNS
After=network.target

[Service]
Type=simple
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file=/run/seleno-hotspot/dnsmasq.conf
Restart=on-failure
RestartSec=3
UNIT
systemctl daemon-reload

TMP=$(mktemp)
echo "$USER_NAME ALL=(root) NOPASSWD: $HELPER" > "$TMP"
visudo -cf "$TMP" >/dev/null
install -m 0440 -o root -g root "$TMP" "$SUDOERS"
rm -f "$TMP"

echo "net.ipv4.ip_unprivileged_port_start=80" > "$SYSCTL"
sysctl -q -p "$SYSCTL"

echo "Done. $USER_NAME's SELENO client can now switch the Pi to its own Wi-Fi in offline mode."
echo "Restart the client; it fetches the telescope's name and certificate from seleno.org while online."
