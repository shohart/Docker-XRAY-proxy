# Xray LAN Proxy Gateway (Docker Compose, TUN, Subscription-based)

## Description

This project runs `xray-core` in Docker and can be used as a LAN proxy gateway.

The stack has 2 services:

- `xray`: main proxy container (HTTP + SOCKS5)
- `updater`: periodic subscription downloader and config replacer

## Important Compatibility Notes

- The updater now accepts only a **full Xray JSON config** from `XRAY_SUBSCRIPTION_URL`.
- Legacy "subscription converters" and simplified JSON formats are not applied automatically.
- If the downloaded payload is not a full Xray config (`inbounds` + `outbounds`), updater keeps the current config and writes an error to log.
- The default config logs to stdout (no file logs). This avoids permission issues on `/var/log/xray` with distroless images.

## Project Structure

```text
.
├── docker-compose.yml
├── .env
├── config/
│   ├── config.json
│   └── example_subscription.json
├── data/
│   └── logs/
├── scripts/
│   └── update_subscription.sh
├── Dockerfile
└── tests/
```

## Environment Variables

`.env` example:

```dotenv
XRAY_SUBSCRIPTION_URL=https://your-provider/config.json
SUB_UPDATE_INTERVAL_MIN=60

LAN_LISTEN_IP=192.168.1.186
HTTP_PROXY_PORT=3128
SOCKS_PROXY_PORT=1080

LAN_CIDR=192.168.1.0/24
GATEWAY_MODE=1
```

Variable description:

- `XRAY_SUBSCRIPTION_URL`: URL that returns full Xray JSON config
- `SUB_UPDATE_INTERVAL_MIN`: update interval in minutes
- `LAN_LISTEN_IP`: host LAN IP (for client configuration)
- `HTTP_PROXY_PORT`: HTTP inbound port
- `SOCKS_PROXY_PORT`: SOCKS inbound port
- `LAN_CIDR`: local subnet
- `GATEWAY_MODE`: transparent gateway mode flag

## Requirements

- Debian 11/12 or another Linux host with Docker
- Docker Engine + Docker Compose v2
- TUN support (`/dev/net/tun`) for transparent mode

Check TUN:

```bash
ls -l /dev/net/tun
```

If missing:

```bash
sudo modprobe tun
```

## Start

```bash
docker compose up -d
```

Check status and logs:

```bash
docker compose ps
docker compose logs -f xray
docker compose logs -f updater
```

Stop:

```bash
docker compose down
```

## Proxy Mode (HTTP/SOCKS)

After startup:

- HTTP proxy: `LAN_LISTEN_IP:HTTP_PROXY_PORT`
- SOCKS5 proxy: `LAN_LISTEN_IP:SOCKS_PROXY_PORT`

Example test:

```bash
curl -x http://192.168.1.186:3128 https://ifconfig.me
```

## Transparent Gateway Mode (TUN)

Enable IPv4 forwarding:

```bash
sudo sysctl -w net.ipv4.ip_forward=1
```

Persist:

```bash
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-xray-gateway.conf
sudo sysctl --system
```

## Xray restart after config updates

After xray-updater got new config it should be reloaded by xray service to be used.
That should be done on the host.

Create a shell script to check if the config changed:

```bash
sudo tee /usr/local/bin/xray-config-watch.sh >/dev/null <<'EOF'
#!/bin/sh
set -eu
PROJECT_DIR="/home/shohart/repositories/Docker-XRAY-proxy"
CONFIG_FILE="$PROJECT_DIR/config/config.json"
STATE_FILE="/var/lib/xray-config-watch/sha256"
mkdir -p /var/lib/xray-config-watch
[ -s "$CONFIG_FILE" ] || exit 0
new="$(sha256sum "$CONFIG_FILE" | awk '{print $1}')"
old="$(cat "$STATE_FILE" 2>/dev/null || true)"
if [ "$new" != "$old" ]; then
  echo "$new" > "$STATE_FILE"
  cd "$PROJECT_DIR"
  docker compose restart xray >/dev/null
fi
EOF
sudo chmod +x /usr/local/bin/xray-config-watch.sh
```

systemd service:

```bash
sudo tee /etc/systemd/system/xray-config-watch.service >/dev/null <<'EOF'
[Unit]
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/xray-config-watch.sh
EOF
```

... and a timer

```bash
sudo tee /etc/systemd/system/xray-config-watch.timer >/dev/null <<'EOF'
[Timer]
OnBootSec=15s
OnUnitActiveSec=10s
AccuracySec=1s
Unit=xray-config-watch.service
[Install]
WantedBy=timers.target
EOF
```

Then read and enable

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now xray-config-watch.timer
```

## Testing

Install test deps:

```bash
pip install -r requirements-tests.txt
```

Run tests:

```bash
python tests/run_all_tests.py
```

## Security Notes

- Do not expose proxy ports to the public internet.
- Restrict inbound access to trusted LAN ranges.

## License

Uses Xray-core (XTLS). Licensing follows upstream project.
