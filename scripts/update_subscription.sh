#!/bin/sh

set -eu

LOG_FILE="/var/log/xray/updater.log"
TARGET_CONFIG="/etc/xray/config.json"
DOWNLOAD_FILE="/tmp/subscription.json"
WORK_CONFIG="/tmp/new-config.json"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG_FILE"
}

if [ -z "${XRAY_SUBSCRIPTION_URL:-}" ]; then
    log "ERROR XRAY_SUBSCRIPTION_URL is not set"
    exit 1
fi

log "INFO Downloading subscription from ${XRAY_SUBSCRIPTION_URL}"
if ! curl -fsSL --connect-timeout 10 --max-time 60 -o "$DOWNLOAD_FILE" "$XRAY_SUBSCRIPTION_URL"; then
    log "ERROR Failed to download subscription"
    exit 1
fi

# Accept only full Xray JSON configs for automatic replacement.
if ! jq -e '.inbounds and .outbounds' "$DOWNLOAD_FILE" >/dev/null 2>&1; then
    log "ERROR Subscription is not a full Xray JSON config (missing inbounds/outbounds); keep current config"
    exit 1
fi

# Keep deterministic local ports even if remote config differs.
jq \
  --argjson http_port "${HTTP_PROXY_PORT:-3128}" \
  --argjson socks_port "${SOCKS_PROXY_PORT:-1080}" \
  '
  .inbounds |= map(
    if .protocol == "http" then .port = $http_port
    elif .protocol == "socks" then .port = $socks_port
    else .
    end
  )
  ' "$DOWNLOAD_FILE" > "$WORK_CONFIG"

if ! jq -e '.inbounds and .outbounds' "$WORK_CONFIG" >/dev/null 2>&1; then
    log "ERROR Generated config failed validation; keep current config"
    exit 1
fi

mv "$WORK_CONFIG" "$TARGET_CONFIG"
cp "$DOWNLOAD_FILE" /etc/xray/subscription.json
log "INFO Subscription converted and config replaced successfully"
