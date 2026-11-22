# Xray LAN Proxy Gateway (Docker Compose)

## Description

Project runs `xray-core` in Docker and supports:

- explicit proxy mode (`HTTP` + `SOCKS5`)
- transparent gateway mode for LAN clients (TCP redirect via `iptables`)
- fail-closed behavior for gateway traffic (no direct internet leak when VPN/proxy path is down)
- proxy bypass rules for domains, domain zones and IP ranges/masks

## Services

- `xray`: main proxy engine
- `balancer`: smart sticky balancer daemon with anti-flapping hysteresis and daily 06:00 AM rotation
- `updater`: downloads subscription and builds final local config with enforced gateway/routing policy
- `gateway`: applies host `iptables` rules for transparent mode and fail-closed forwarding

## How It Works

1. `updater` downloads `XRAY_SUBSCRIPTION_URL`.
1. If payload is full Xray JSON, it is used as source; otherwise links are extracted via `scripts/html2xray.py`.
1. `scripts/compose_xray_config.py` builds final `config/config.json`:
   - local inbounds (HTTP/SOCKS + optional transparent `dokodemo-door`)
   - outbounds from subscription (proxy nodes), plus `direct` and `block` if missing
   - routing rules: local/private + bypass rules -> `direct`, all else -> proxy outbounds
   - if subscription has multiple proxy outbounds, `proxy-auto` balancer is added and used as default route
   - `observatory` is enabled to continuously probe all proxy outbounds and keep health state fresh
   - default balancer strategy is `random`, so client requests are spread randomly across healthy nodes
1. `balancer` service runs the Smart Sticky Balancer daemon:
   - Pins traffic to an active "sticky node" via Xray API override to prevent IP flapping.
   - Monitors observatory metrics via gRPC (`127.0.0.1:10085`).
   - Fails over immediately only when the active node is dead (`consecutive_failures >= 2`).
   - Hysteresis: ignores small latency fluctuations; switches only if an alternative node is substantially better (>= 50% faster, >= 100ms delta) for multiple checks.
   - Smoothly rotates to the best available node once a day at a randomized time around 6:00 AM (05:30 - 06:30).
   - Check status anytime via `scripts/tunnelctl status`.
1. In `GATEWAY_MODE=1`, `gateway` service installs `iptables` rules:
   - transparently intercept LAN traffic with `TPROXY` (TCP+UDP)
   - install `ip rule` + routing table for marked packets (`local 0.0.0.0/0 dev lo`)
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

XRAY_BALANCER_STRATEGY=random
XRAY_BALANCER_FALLBACK_TAG=block
XRAY_PROBE_URL=https://www.google.com/generate_204
XRAY_PROBE_INTERVAL=20s
XRAY_PROBE_CONCURRENCY=1
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
- `GATEWAY_DNS_SERVER`: trusted LAN DNS server (e.g. Technitium DNS at `192.168.1.123`) to avoid poisoned provider DNS
- `GATEWAY_REDIRECT_DNS`: `1` redirects LAN DNS (port 53 UDP/TCP) with hairpin NAT to `GATEWAY_DNS_SERVER`, `0` leaves DNS unredirected
- `GATEWAY_BLOCK_QUIC`: `1` drops UDP 443 (QUIC) so clients instantly fall back to HTTP/2 over TLS (prevents stalls over VLESS/DPI)
- `BYPASS_DOMAINS`: exact domains that should go `direct`
- `BYPASS_DOMAIN_ZONES`: domain suffixes/zones that should go `direct`
- `BYPASS_IP_CIDRS`: CIDR ranges that should go `direct`
- `BYPASS_IP_MASKS`: wildcard IPv4 masks (`*` only in trailing octets), converted to CIDR
- `XRAY_BALANCER_STRATEGY`: balancing mode for multiple proxy nodes (`random`, `roundRobin`, `leastPing`, `leastLoad`)
- `XRAY_BALANCER_FALLBACK_TAG`: outbound used when all proxy nodes are down (`block` recommended for fail-closed)
- `XRAY_PROBE_URL`: probe target used by Xray observatory to test outbound quality/availability
- `XRAY_PROBE_INTERVAL`: observatory probe interval
- `XRAY_PROBE_CONCURRENCY`: `1/true` enables concurrent probing, `0/false` disables

## Requirements

- Linux host with Docker + Docker Compose v2
- Host kernel with `TPROXY` support

## Host Configuration (Required for Gateway Mode)

Apply once:

```bash
sudo modprobe nf_tproxy_core
sudo modprobe xt_TPROXY
sudo modprobe xt_socket

sudo tee /etc/sysctl.d/99-xray-gateway.conf >/dev/null <<'EOF'
net.ipv4.ip_forward=1
net.ipv4.conf.all.rp_filter=0
net.ipv4.conf.default.rp_filter=0
net.ipv4.conf.all.src_valid_mark=1
EOF

sudo sysctl --system
```

Persist kernel modules across reboot:

```bash
sudo tee /etc/modules-load.d/xray-gateway.conf >/dev/null <<'EOF'
nf_tproxy_core
xt_TPROXY
xt_socket
EOF
```

Verify after `docker compose up -d`:

```bash
ip rule show
ip route show table 100
iptables -t mangle -S XRAY_GW
docker compose logs --tail=100 gateway
docker compose logs --tail=100 xray
```

Expected:

- `ip rule` contains `fwmark 0x1 lookup 100`
- `table 100` contains `local 0.0.0.0/0 dev lo`
- xray log does **not** contain `failed to set IP_TRANSPARENT`

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

## tunnelctl — nodes, clients, rotation rules

`scripts/tunnelctl` is the control panel (CLI + curses TUI, stdlib only). Run it on
the host; bare `scripts/tunnelctl` in a terminal opens the TUI (`--ascii` disables
flag emoji).

```sh
tunnelctl nodes --sort delay          # nodes with 🇳🇱 country, RTT, who uses them
tunnelctl use node_11 | use -c DE     # pin ALL traffic to a node / best node of a country
tunnelctl next [-c NL]                # next available node (optionally inside a country)
tunnelctl auto | rotate               # automatic sticky mode / rotate right now
tunnelctl policy only NL DE           # rotate ONLY across these countries
tunnelctl policy except RU            # all countries EXCEPT these   (policy all = reset)
tunnelctl rotation 6h -s random       # off | daily | daily@5 | 30m | 6h | 1d; best|next|random
tunnelctl clients                     # active LAN clients (from the xray access log)
tunnelctl pin 192.168.1.50 node_3     # pin one client to a node
tunnelctl pin 192.168.1.50 -c PL      # pin one client to a country (sticky + failover inside it)
tunnelctl next --client 192.168.1.50  # every lane command takes --client
tunnelctl unpin 192.168.1.50
tunnelctl country node1 NL            # fix a wrongly detected country
```

How it fits together:

- **Countries** come from provider remarks (`🇳🇱 Netherlands 1`, `GhostLine 🇫🇮 Finland`):
  a country *name* beats the flag (`🇷🇺 МОСТ Netherlands` exits in NL), then the flag,
  then tag/host hints (`ams_1`, `bridge-nl2…`). The updater writes them to
  `config/nodes_meta.json` (never into `config.json`, so it does not restart xray).
- **Control file** `config/tunnel_policy.json` is written by tunnelctl and watched by
  the sticky balancer daemon (applies changes within a second). It has a `global`
  lane and one lane per pinned client: `mode` auto/manual, `node`, `filter`
  (`all`/`only`/`except` + countries), `rotation` (schedule + strategy), `failover`.
- **Manual** pins hold until `auto`; if the node dies the lane fails over inside its
  rules and returns when the node is back. If no allowed node is alive the lane uses
  the best alive node anyway and `status` shows a ⚠ policy violation (soft rules).
- **Per-client pins** = a balancer `client-<ip>` + a `source` rule placed before the
  catch-all rule (bypass/direct rules still win). `pin`/`unpin` of a *new* client
  hot-swaps the routing of the running xray (no restart); the updater bakes the
  pins into `config.json` on its next run, which restarts xray once.
  `tunnelctl apply --persist` does that immediately; `tunnelctl reconcile` re-applies
  pins if xray restarted from a config without them (cron-friendly).
- The daemon dumps node latencies to `data/sticky_state.json`; tunnelctl needs no gRPC.

## Multi-Server Behavior

When subscription provides multiple proxy servers:

- all proxy outbounds stay under continuous observatory probing
- unhealthy nodes are excluded by balancer decisions
- healthy nodes receive traffic by selected strategy (`random` by default)
- fallback is controlled by `XRAY_BALANCER_FALLBACK_TAG` (default `block` to avoid leaks)

## Testing

```bash
pip install -r requirements-tests.txt
python tests/run_all_tests.py
```

## Security

- Do not expose proxy ports to the public internet.
- Restrict access to trusted LAN ranges.
