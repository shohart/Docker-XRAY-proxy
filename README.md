# Xray LAN Proxy Gateway (Docker Compose, TUN, Subscription-based)

## Description

This project deploys a Docker container based on Xray-core
(https://github.com/XTLS/Xray-core) and turns a Debian server into a
full-featured VPN gateway for a local network.

The container:

-   Works as an explicit proxy (HTTP + SOCKS5) for LAN devices
-   Can operate as a transparent gateway using TUN
-   Reads Xray subscription URL from `.env`
-   Supports any protocol present in subscription (VLESS, VMess, Trojan,
    Shadowsocks, etc.)
-   Periodically updates subscription and reloads configuration
-   Designed for Debian + Docker Compose

## Implementation Status

This project has been implemented with the following structure:

-   `docker-compose.yml` - Main Docker Compose configuration
-   `.env` - Environment variables file
-   `config/` - Configuration files directory
-   `data/` - Data and logs directory  
-   `scripts/` - Utility scripts
-   `Dockerfile` - Docker build configuration

## Project Structure

    .
    ├── docker-compose.yml
    ├── .env
    ├── config/
    │   ├── config.json          # Main Xray configuration
    │   └── example_subscription.json  # Example subscription format
    ├── data/
    │   └── logs/                # Log files directory
    ├── scripts/
    │   └── update_subscription.sh  # Subscription update script
    └── tests/
        ├── test_docker_build.py     # Docker build test
        ├── test_structure.py        # Tests project structure and file existence
        ├── test_config_validation.py # Validates configuration files
        ├── test_env_validation.py   # Verifies environment variables
        └── run_all_tests.py         # Script to run all tests

## Configuration

### Environment Variables (.env)

Create `.env`:

    XRAY_SUBSCRIPTION_URL=https://your-provider/subscription
    SUB_UPDATE_INTERVAL_MIN=60

    LAN_LISTEN_IP=192.168.1.186
    HTTP_PROXY_PORT=3128
    SOCKS_PROXY_PORT=1080

    LAN_CIDR=192.168.1.0/24
    GATEWAY_MODE=1

Variable description:

XRAY_SUBSCRIPTION_URL -- Xray subscription link\
SUB_UPDATE_INTERVAL_MIN -- update interval (minutes)\
LAN_LISTEN_IP -- Debian host LAN IP\
HTTP_PROXY_PORT -- HTTP proxy port\
SOCKS_PROXY_PORT -- SOCKS5 proxy port\
LAN_CIDR -- local network subnet\
GATEWAY_MODE -- enable transparent gateway (1 = enabled)

## Architecture

Traffic flow:

LAN Device\
→ Debian Host\
→ Docker Container (Xray)\
→ TUN Interface\
→ VPN Server (from subscription)\
→ Internet

Two supported modes:

1.  Explicit Proxy (HTTP/SOCKS5)
2.  Transparent Gateway (TUN routing)

## Requirements

-   Debian 11 or 12
-   Docker Engine
-   Docker Compose v2
-   Kernel TUN support (/dev/net/tun)

Check TUN:

    ls -l /dev/net/tun

If missing:

    sudo modprobe tun

## Start

    docker compose up -d

Check logs:

    docker compose logs -f xray

Stop:

    docker compose down

## Mode 1: Explicit Proxy

After startup:

HTTP proxy: LAN_LISTEN_IP:HTTP_PROXY_PORT\
SOCKS5 proxy: LAN_LISTEN_IP:SOCKS_PROXY_PORT

Configure proxy manually on client devices.

Test:

    curl -x http://192.168.1.186:3128 https://ifconfig.me

## Mode 2: Transparent Gateway (TUN)

### Enable forwarding

    sudo sysctl -w net.ipv4.ip_forward=1

Persist:

    echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-xray-gateway.conf
    sudo sysctl --system

### Set Debian as Gateway

-   Configure router DHCP default gateway OR
-   Set manually on devices

### Ensure docker-compose includes:

    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun

After configuration, LAN traffic will pass through Xray tunnel.

## Subscription Update

Subscription updates every SUB_UPDATE_INTERVAL_MIN minutes:

1.  Download subscription
2.  Convert to Xray JSON
3.  Reload or restart Xray

Check updater logs:

    docker compose logs -f updater

## Security

-   Do NOT expose proxy ports to the internet
-   Restrict access to LAN only

Example iptables:

    sudo iptables -A INPUT -p tcp -s 192.168.1.0/24 --dport 3128 -j ACCEPT
    sudo iptables -A INPUT -p tcp -s 192.168.1.0/24 --dport 1080 -j ACCEPT
    sudo iptables -A INPUT -p tcp --dport 3128 -j DROP
    sudo iptables -A INPUT -p tcp --dport 1080 -j DROP

## Troubleshooting

Check containers:

    docker compose ps

Check logs:

    docker compose logs -f

Check routes:

    ip route

Check interfaces:

    ip addr

## License

Uses Xray-core (XTLS). Licensing follows upstream project.

## Testing

This project includes automated tests to verify proper functionality:

- `tests/test_docker_build.py` - Tests Docker build configuration
- `tests/test_structure.py` - Tests project structure and file existence
- `tests/test_config_validation.py` - Validates configuration files
- `tests/test_env_validation.py` - Verifies environment variables
- `tests/run_all_tests.py` - Script to run all tests

To run tests:
```bash
cd XRAY-PROXY-Container
python3 tests/run_all_tests.py
```

## Test Requirements

The test suite requires the following Python packages:
- `PyYAML>=5.4.1`
- `pytest>=6.2.4` 
- `jsonschema>=3.2.0`

Install test dependencies:
```bash
pip install -r requirements-tests.txt