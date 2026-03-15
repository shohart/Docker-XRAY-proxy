# Xray LAN Proxy Gateway + 3x-ui (Docker Compose)

## Description

Проект запускает `xray-core` в Docker и поддерживает:

- explicit proxy mode (`HTTP` + `SOCKS5`);
- transparent gateway mode для LAN-клиентов (TPROXY через `iptables`);
- fail-closed поведение для gateway-трафика (без утечки в прямой интернет при проблемах proxy/VPN-пути);
- bypass-правила по доменам, доменным зонам и IP-диапазонам;
- изолированный control-plane контейнер `3x-ui` (панель администрирования), не включённый в data-plane маршрутизацию.

## Services

- `xray`: основной прокси-движок.
- `updater`: получает подписку, собирает финальный локальный config и применяет его через single-writer apply pipeline.
- `gateway`: применяет `iptables`/`ip rule`/`ip route` правила для transparent mode и fail-closed forwarding.
- `xui`: контейнер панели `3x-ui` для control-plane (отдельная сеть `control-plane`, отдельный volume `xui-db`).

## 3x-ui Integration Boundaries (важно)

Интеграция `3x-ui` в текущей реализации:

1. добавляет сервис `xui` в `docker-compose`;
2. публикует панель на `${THREEX_UI_BIND_IP}:${THREEX_UI_PORT}` (контейнерный порт `2053/tcp`);
3. хранит данные панели в named volume `xui-db`;
4. запускается с ограничениями least privilege (`cap_drop: [ALL]`, `no-new-privileges`, без `network_mode: host`);
5. не получает полный project `.env` (включая `XRAY_SUBSCRIPTION_URL`): переменные `THREEX_UI_*` используются только на уровне compose interpolation.

При этом `xui` не участвует в построении data-plane-конфига `xray`:
источник истины для `config/config.json` — pipeline `update_subscription.sh` -> `compose_xray_config.py` -> `apply_xray_config.py`.

## How It Works

1. `updater` скачивает `XRAY_SUBSCRIPTION_URL`.
2. Если payload уже полноценный Xray JSON (`.inbounds` + `.outbounds`) — используется как source; иначе ссылки извлекаются через `scripts/html2xray.py`.
3. `scripts/compose_xray_config.py` строит финальный `config/config.json`:
   - локальные inbounds (`http`, `socks`, а при `GATEWAY_MODE=1` — `dokodemo-door`);
   - proxy outbounds из подписки;
   - гарантирует наличие `direct` и `block`;
   - добавляет routing:
     - local/private + bypass -> `direct`,
     - остальное -> proxy path (single outbound или balancer `proxy-auto`).
4. Для multi-node подписки:
   - включается balancer (`XRAY_BALANCER_STRATEGY`);
   - включается `observatory` (`XRAY_PROBE_*`);
   - fallback задаётся `XRAY_BALANCER_FALLBACK_TAG` (рекомендуется `block` для fail-closed).
5. `scripts/apply_xray_config.py` валидирует candidate, берёт lock, атомарно заменяет target-файл и сохраняет текущий конфиг при ошибках.

## Environment Variables

Скопируйте пример и отредактируйте значения:

```bash
cp .env.example .env
```

### Core variables (data-plane)

```dotenv
XRAY_SUBSCRIPTION_URL=https://your-provider/config.json
XRAY_IMAGE=ghcr.io/xtls/xray-core:26.2.6
SUB_UPDATE_INTERVAL_MIN=60
XRAY_SAVE_RAW_SUBSCRIPTION=0

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

Дополнительно для стратегии `leastLoad` можно использовать:
`XRAY_BALANCER_EXPECTED`, `XRAY_BALANCER_MAX_RTT`, `XRAY_BALANCER_TOLERANCE`,
`XRAY_BALANCER_BASELINES`, `XRAY_BALANCER_COSTS` (см. `.env.example`).

### New 3x-ui variables (control-plane)

- `THREEX_UI_IMAGE` — образ `3x-ui` (используйте pinned tag, например `ghcr.io/mhsanaei/3x-ui:v2.5.2`).
- `THREEX_UI_BIND_IP` — bind-IP панели на хосте (по умолчанию `127.0.0.1`).
- `THREEX_UI_PORT` — порт панели на хосте (по умолчанию `2053`).

Рекомендация по безопасности: оставляйте `THREEX_UI_BIND_IP=127.0.0.1`, если не нужен внешний доступ.

## Requirements

- Linux host с Docker + Docker Compose v2.
- Для `GATEWAY_MODE=1`: ядро/модули с `TPROXY` support.

## Host Configuration (required for Gateway Mode)

Применяется один раз:

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

Сохранить модули после reboot:

```bash
sudo tee /etc/modules-load.d/xray-gateway.conf >/dev/null <<'EOF'
nf_tproxy_core
xt_TPROXY
xt_socket
EOF
```

## Start / Update Containers (после интеграции 3x-ui)

Первичный запуск или обновление стека:

```bash
docker compose pull
docker compose up -d
```

Проверка состояния:

```bash
docker compose ps
docker compose logs --tail=100 xray
docker compose logs --tail=100 updater
docker compose logs --tail=100 gateway
docker compose logs --tail=100 xui
```

Если менялся только `.env`/compose-конфиг, в большинстве случаев достаточно снова выполнить:

```bash
docker compose up -d
```

## Runtime Validation

### 1) Compose + healthcheck

```bash
docker compose ps
docker compose ps xui
```

Ожидаемо:

- `xray`, `updater`, `gateway`, `xui` запущены;
- для `xui` отображается `healthy` (healthcheck в compose).

### 2) Проверка apply pipeline

Запустить цикл обновления вручную:

```bash
docker compose exec updater /bin/sh /scripts/update_subscription.sh
```

Проверить лог updater:

```bash
docker compose logs --tail=200 updater
```

Ожидаемые маркеры в логах:

- `INFO Downloading subscription`
- один из вариантов:
  - `INFO Full Xray JSON detected; normalizing inbound ports`
  - `INFO Not a full Xray JSON; trying to extract links ...`
- `INFO Config applied atomically: /etc/xray/config.json` **или** `INFO Config unchanged; no replace`
- `INFO Apply pipeline finished`
- `INFO Done`

Валидация итогового конфига движком Xray:

```bash
docker compose exec xray xray run -test -c /etc/xray/config.json
```

### 3) Pytest (регрессионная валидация)

```bash
pip install -r requirements-tests.txt
python -m pytest tests/ -v --tb=short
```

или эквивалентно:

```bash
python tests/run_all_tests.py
```

## Mode-Specific Checks

### Proxy mode

HTTP/SOCKS endpoints:

- HTTP: `LAN_LISTEN_IP:HTTP_PROXY_PORT`
- SOCKS5: `LAN_LISTEN_IP:SOCKS_PROXY_PORT`

Проверка HTTP:

```bash
curl -x http://192.168.1.186:3128 https://ifconfig.me
```

Проверка SOCKS5:

```bash
curl --socks5 192.168.1.186:1080 https://ifconfig.me
```

### Gateway mode + fail-closed

Проверки на хосте:

```bash
ip rule show
ip route show table 100
iptables -t mangle -S XRAY_GW
iptables -S XRAY_GW_FWD
docker compose logs --tail=100 gateway
docker compose logs --tail=100 xray
```

Ожидаемо:

- в `ip rule` есть `fwmark 0x1 lookup 100` (или эквивалент с mark `1`);
- в table `100` есть `local 0.0.0.0/0 dev lo`;
- chain `XRAY_GW` присутствует в `mangle`;
- chain `XRAY_GW_FWD` заканчивается `REJECT` (fail-closed);
- в логе xray нет `failed to set IP_TRANSPARENT`.

## Regression Control Checklist

### A. Proxy mode

- сервисы подняты (`docker compose ps`);
- HTTP/SOCKS запросы через прокси проходят;
- порты соответствуют `HTTP_PROXY_PORT`/`SOCKS_PROXY_PORT`.

### B. Gateway mode

- `GATEWAY_MODE=1`, задан `LAN_CIDR`;
- применились `ip rule` + route table `100`;
- установлены `iptables` chain `XRAY_GW` и `XRAY_GW_FWD`;
- fail-closed активен (`XRAY_GW_FWD` содержит финальный `REJECT`).

### C. Subscription / apply / fail-closed behavior

- updater корректно обрабатывает:
  - full JSON subscription;
  - non-JSON payload через `html2xray.py`;
- apply pipeline даёт только атомарное применение (`apply_xray_config.py`);
- при ошибках candidate текущий `config.json` сохраняется (fail-closed update behavior);
- в финальном конфиге присутствует `block` outbound;
- для multi-node routing fallback остаётся `block` (рекомендуемо не менять);
- raw payload подписки по умолчанию не сохраняется; для диагностики включается только через `XRAY_SAVE_RAW_SUBSCRIPTION=1` (путь `data/raw/subscription.raw`).

### D. Control-plane (3x-ui)

- `xui` изолирован в `control-plane`;
- у `xui` нет `NET_ADMIN`/`NET_RAW`, включён `no-new-privileges`, capabilities dropped;
- `xui` не получает полный project `.env`; `THREEX_UI_*` используются как compose-level настройки interpolation;
- panel bind ограничен (`THREEX_UI_BIND_IP=127.0.0.1`, если внешний доступ не нужен).

## Safe Operation Rules (что нельзя менять без понимания последствий)

1. Не публикуйте `3x-ui` в интернет без ACL/VPN/reverse-proxy hardening.
2. Не добавляйте `xui` в host network и не выдавайте ему лишние capabilities.
3. Не убирайте fail-closed semantics:
   - `block` outbound,
   - `XRAY_BALANCER_FALLBACK_TAG=block` (для multi-outbound).
4. Не включайте `GATEWAY_MODE=1` без корректного `LAN_CIDR` и host TPROXY prerequisites.
5. Не редактируйте вручную `config/config.json` при запущенном `updater`:
   изменения будут перезаписаны очередным циклом.
6. После изменения data-plane конфига перезапускайте `xray` (hot reload не выполняется автоматически).

## Rollback / Restore

### Быстрый rollback control-plane (3x-ui) без остановки data-plane

```bash
docker compose stop xui
```

Возврат панели:

```bash
docker compose start xui
```

### Restore data-plane конфига

Перед рискованными изменениями сохраняйте backup:

```bash
cp ./config/config.json ./config/config.json.backup
```

Восстановление:

```bash
docker compose stop updater
cp ./config/config.json.backup ./config/config.json
docker compose exec xray xray run -test -c /etc/xray/config.json
docker compose restart xray
docker compose start updater
```

Примечание: если apply pipeline не смог применить новый candidate, текущий конфиг по дизайну уже сохранён автоматически.

## Notes About Xray Reload

`xray` контейнер не делает hot-reload `config.json` автоматически.
Если конфиг изменился, перезапустите сервис:

```bash
docker compose restart xray
```

## Stop

```bash
docker compose down
```

## Security

- Не открывайте proxy-порты в публичный интернет.
- Ограничивайте доступ к proxy и panel только доверенным сегментам/адресам.
- Для панели `3x-ui` по умолчанию используйте loopback bind (`127.0.0.1`).
