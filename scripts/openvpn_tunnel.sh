#!/bin/sh
# openvpn_tunnel.sh
# Backup egress tunnel for the xray-proxy stack.
#
# - Connects an OpenVPN client from OVPN_CONFIG (self-contained .ovpn).
# - Forces ALL egress through the tunnel (redirect-gateway def1).
# - Exposes a SOCKS5 proxy (dante/sockd) bound to the tun device so xray can
#   dial it as an additional balancer outbound.
# - Installs a fail-closed kill-switch: nothing leaves the container except the
#   OpenVPN control channel and traffic that rides tun0. If the tunnel drops,
#   the SOCKS proxy cannot leak to the bare bridge network.
#
# Runs as PID-ish entrypoint: openvpn in background, sockd in foreground.
set -eu

OVPN_CONFIG="${OVPN_CONFIG:-/etc/openvpn/client.ovpn}"
SOCKS_PORT="${OPENVPN_SOCKS_LISTEN_PORT:-1080}"
TUN_DEV="tun0"
SOCKD_CONF="/tmp/sockd.conf"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1"; }

require_config() {
  if [ ! -f "$OVPN_CONFIG" ]; then
    log "ERROR OpenVPN config not found at $OVPN_CONFIG"
    exit 1
  fi
}

# Install tooling (mirrors the gateway/updater services which apk-add at start).
apk add --no-cache openvpn dante-server iptables iproute2 curl >/dev/null

require_config

# Derive the VPN control endpoint from the config so the kill-switch can allow
# exactly that one connection out of the bare interface.
REMOTE_LINE="$(grep -E '^[[:space:]]*remote[[:space:]]' "$OVPN_CONFIG" | head -1)"
VPN_IP="$(echo "$REMOTE_LINE" | awk '{print $2}')"
VPN_PORT="$(echo "$REMOTE_LINE" | awk '{print $3}')"
case "$(grep -E '^[[:space:]]*proto' "$OVPN_CONFIG" | head -1)" in
  *udp*) VPN_PROTO="udp" ;;
  *)     VPN_PROTO="tcp" ;;
esac
if [ -z "$VPN_IP" ] || [ -z "$VPN_PORT" ]; then
  log "ERROR Could not parse 'remote <ip> <port>' from $OVPN_CONFIG"
  exit 1
fi
log "INFO VPN control channel: $VPN_PROTO $VPN_IP:$VPN_PORT, socks port $SOCKS_PORT"

# ---------------------------------------------------------------------------
# Kill-switch (fail-closed). Applied BEFORE the tunnel is up.
# ---------------------------------------------------------------------------
iptables -F
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT DROP

# Loopback + already-established flows.
iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A INPUT  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Accept SOCKS clients (xray) coming from the docker bridge.
iptables -A INPUT -p tcp --dport "$SOCKS_PORT" -j ACCEPT
iptables -A INPUT -p udp --dport "$SOCKS_PORT" -j ACCEPT

# The one hole in the bare interface: the OpenVPN control channel itself.
iptables -A OUTPUT -d "$VPN_IP" -p "$VPN_PROTO" --dport "$VPN_PORT" -j ACCEPT

# Everything else may only leave through the tunnel.
iptables -A OUTPUT -o "$TUN_DEV" -j ACCEPT

# Docker embedded DNS (127.0.0.11) for resolving nothing external, harmless.
iptables -A OUTPUT -d 127.0.0.11 -j ACCEPT
iptables -A INPUT  -s 127.0.0.11 -j ACCEPT

log "INFO Kill-switch installed (default-deny egress except tun0 + control channel)"

# ---------------------------------------------------------------------------
# Start OpenVPN. redirect-gateway def1 forces the default route through tun0 so
# the SOCKS proxy egress is tunneled even if the server does not push it.
# ---------------------------------------------------------------------------
log "INFO Starting OpenVPN"
openvpn --config "$OVPN_CONFIG" \
        --redirect-gateway def1 \
        --dev "$TUN_DEV" \
        --daemon --log /tmp/openvpn.log \
        --writepid /tmp/openvpn.pid

# Wait for the tunnel interface to appear and get an address.
log "INFO Waiting for $TUN_DEV to come up"
for i in $(seq 1 60); do
  if ip link show "$TUN_DEV" >/dev/null 2>&1 && ip -4 addr show "$TUN_DEV" | grep -q 'inet '; then
    log "INFO $TUN_DEV is up: $(ip -4 addr show "$TUN_DEV" | awk '/inet /{print $2}')"
    break
  fi
  if [ "$i" = "60" ]; then
    log "ERROR $TUN_DEV did not come up in time; dumping openvpn log"
    cat /tmp/openvpn.log 2>/dev/null || true
    exit 1
  fi
  sleep 1
done

# ---------------------------------------------------------------------------
# SOCKS5 proxy (dante). internal = listen for xray; external = egress via tun0.
# ---------------------------------------------------------------------------
cat > "$SOCKD_CONF" <<EOF
logoutput: stderr
internal: 0.0.0.0 port = $SOCKS_PORT
external: $TUN_DEV
socksmethod: none
clientmethod: none
user.unprivileged: sockd
client pass {
    from: 0.0.0.0/0 to: 0.0.0.0/0
}
socks pass {
    from: 0.0.0.0/0 to: 0.0.0.0/0
}
EOF

# dante needs an unprivileged user to drop to.
id sockd >/dev/null 2>&1 || adduser -D -H -s /sbin/nologin sockd >/dev/null 2>&1 || true

log "INFO Starting SOCKS5 proxy on 0.0.0.0:$SOCKS_PORT (egress via $TUN_DEV)"

# Keep the tunnel supervised: if openvpn dies, take the whole container down so
# restart:unless-stopped brings it back cleanly (and no leak in the meantime).
( while kill -0 "$(cat /tmp/openvpn.pid 2>/dev/null)" 2>/dev/null; do sleep 5; done
  log "ERROR OpenVPN process exited; stopping container"
  kill 1 2>/dev/null || true ) &

exec sockd -f "$SOCKD_CONF"
