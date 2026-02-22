#!/bin/sh
set -eu

LOG_FILE="/var/log/xray/updater.log"
TARGET_CONFIG="/etc/xray/config.json"
DOWNLOAD_FILE="/tmp/subscription.body"
WORK_CONFIG="/tmp/new-config.json"

log() {
    msg="$(date '+%Y-%m-%d %H:%M:%S') $1"
    echo "$msg"
    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

sha256_file() {
    [ -f "$1" ] && sha256sum "$1" | awk '{print $1}' || echo ""
}

if [ -z "${XRAY_SUBSCRIPTION_URL:-}" ]; then
    log "ERROR XRAY_SUBSCRIPTION_URL is not set"
    exit 1
fi

log "INFO Downloading subscription"
if ! curl -fsSL --connect-timeout 10 --max-time 60 -o "$DOWNLOAD_FILE" "$XRAY_SUBSCRIPTION_URL"; then
    log "ERROR Failed to download subscription"
    exit 1
fi

old_sum="$(sha256_file "$TARGET_CONFIG")"

head_bytes="$(head -c 200 "$DOWNLOAD_FILE" | tr '\n' ' ' | tr -d '\r')"
case "$head_bytes" in
    *"<html"*|*"<!DOCTYPE html"*|*"User Information"*)
        log "INFO HTML detected; extracting links and generating Xray config"
        if /scripts/html2xray.py "$DOWNLOAD_FILE" "$WORK_CONFIG"; then
            new_sum="$(sha256_file "$WORK_CONFIG")"
            if [ -n "$new_sum" ] && [ "$new_sum" != "$old_sum" ]; then
                mv "$WORK_CONFIG" "$TARGET_CONFIG"
                cp "$DOWNLOAD_FILE" /etc/xray/subscription.html 2>/dev/null || true
                log "INFO Config updated"
            else
                log "INFO Config unchanged"
                rm -f "$WORK_CONFIG" 2>/dev/null || true
            fi
            exit 0
        else
            log "ERROR Failed to convert HTML to Xray config"
            exit 1
        fi
    ;;
esac

if ! jq -e '.inbounds and .outbounds' "$DOWNLOAD_FILE" >/dev/null 2>&1; then
    log "ERROR Not a full Xray JSON config; keep current config"
    exit 1
fi

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
    log "ERROR Generated config failed validation"
    exit 1
fi

new_sum="$(sha256_file "$WORK_CONFIG")"
if [ -n "$new_sum" ] && [ "$new_sum" != "$old_sum" ]; then
    mv "$WORK_CONFIG" "$TARGET_CONFIG"
    cp "$DOWNLOAD_FILE" /etc/xray/subscription.json 2>/dev/null || true
    log "INFO Config updated"
else
    log "INFO Config unchanged"
    rm -f "$WORK_CONFIG" 2>/dev/null || true
fi