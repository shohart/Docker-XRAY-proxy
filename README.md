# Xray LAN Proxy Gateway (Docker Compose)

## Description

Project runs `xray-core` in Docker and supports:

- explicit proxy mode (`HTTP` + `SOCKS5`)
- transparent gateway mode for LAN clients (TCP redirect via `iptables`)
- fail-closed behavior for gateway traffic (no direct internet leak when VPN/proxy path is down)
- proxy bypass rules for domains, domain zones and IP ranges/masks

## Services

- `xray`: main proxy engine
- `updater`: downloads subscription and builds final local config with enforced gateway/routing policy
- `gateway`: applies host `iptables` rules for transparent mode and fail-closed forwarding

## How It Works

1. `updater` downloads `XRAY_SUBSCRIPTION_URL`.
1. If payload is full Xray JSON, it is used as source; otherwise links are extracted via `scripts/html2xray.py`.
1. `scripts/compose_xray_config.py` builds final `config/config.json`:
   - local inbounds (HTTP/SOCKS + optional transparent `dokodemo-door`)
   - outbounds from subscription (proxy nodes), plus `direct` and `block` if missing
   - routing rules: local/private + bypass rules -> `direct`, all else -> proxy outbounds
   - if subscription has multiple proxy outbounds, automatic balancer `proxy-auto` is added and used as default route
1. In `GATEWAY_MODE=1`, `gateway` service installs `iptables` rules:
   - redirect LAN TCP traffic to transparent inbound port
   - block direct forwarded internet traffic by default (fail-closed)
   - allow explicit bypass CIDRs/masks to go direct

## Environment Variables

Example:

```dotenv
XRAY_SUBSCRIPTION_URL=https://your-provider/config.json
XRAY_IMAGE=ghcr.io/xtls/xray-core:26.2.6
SUB_UPDATE_INTERVAL_MIN=60

LAN_LISTEN_IP=192.168.1.186
HTTP_PROXY_PORT=3128
SOCKS_PROXY_PORT=1080

LAN_CIDR=192.168.1.0/24
GATEWAY_MODE=1
GATEWAY_TPROXY_PORT=12345

BYPASS_DOMAINS=example.com,api.example.com
BYPASS_DOMAIN_ZONES=.local,.corp,example.org
BYPASS_IP_CIDRS=203.0.113.0/24
BYPASS_IP_MASKS=198.51.100.*,203.0.*.*
```

Variables:

- `XRAY_SUBSCRIPTION_URL`: source subscription/config URL
- `XRAY_IMAGE`: pinned Xray image tag used by `xray` service
- `SUB_UPDATE_INTERVAL_MIN`: updater interval in minutes
- `LAN_LISTEN_IP`: host LAN IP for clients
- `HTTP_PROXY_PORT`: HTTP proxy port
- `SOCKS_PROXY_PORT`: SOCKS5 proxy port
- `LAN_CIDR`: LAN subnet used for transparent gateway matching
- `GATEWAY_MODE`: `1` enables transparent gateway and firewall rules, `0` disables
- `GATEWAY_TPROXY_PORT`: local transparent inbound port (must match Xray config)
- `BYPASS_DOMAINS`: exact domains that should go `direct`
- `BYPASS_DOMAIN_ZONES`: domain suffixes/zones that should go `direct`
- `BYPASS_IP_CIDRS`: CIDR ranges that should go `direct`
- `BYPASS_IP_MASKS`: wildcard IPv4 masks (`*` only in trailing octets), converted to CIDR

## Requirements

- Linux host with Docker + Docker Compose v2
- IPv4 forwarding enabled for gateway mode:

```bash
sudo sysctl -w net.ipv4.ip_forward=1
```

Persist:

```bash
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-xray-gateway.conf
sudo sysctl --system
```

## Start

```bash
docker compose up -d
```

Check:

```bash
docker compose ps
docker compose logs -f xray
docker compose logs -f updater
docker compose logs -f gateway
```

Stop:

```bash
docker compose down
```

## Proxy Mode

- HTTP: `LAN_LISTEN_IP:HTTP_PROXY_PORT`
- SOCKS5: `LAN_LISTEN_IP:SOCKS_PROXY_PORT`

Example:

```bash
curl -x http://192.168.1.186:3128 https://ifconfig.me
```

## Gateway Fail-Closed Behavior

When `GATEWAY_MODE=1`, forwarded LAN internet traffic is denied unless:

- it is transparently redirected into Xray, or
- destination is in explicit bypass CIDRs/masks, or
- destination stays inside LAN/private ranges

This prevents accidental direct internet leakage when the VPN/proxy path is unavailable.

## Notes About Xray Reload

Xray container does not hot-reload `config.json` automatically. If config file changes, restart `xray`:

```bash
docker compose restart xray
```

Use a host-side watcher/timer if automatic restart after config update is required.

## Testing

```bash
pip install -r requirements-tests.txt
python tests/run_all_tests.py
```

## Security

- Do not expose proxy ports to the public internet.
- Restrict access to trusted LAN ranges.
