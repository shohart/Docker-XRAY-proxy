#!/bin/sh
# update_subscription.sh
# - Downloads XRAY_SUBSCRIPTION_URL payload
# - If payload is full Xray JSON (has .inbounds and .outbounds) -> use as source
# - Else (HTML / text / base64 subscription) -> extract links via html2xray.py and generate source Xray config
# - Compose final local config via compose_xray_config.py (gateway/routing/bypass policy)
# - Uses atomic replace (mv) and validates JSON with jq before replacing
# - Logs to stdout + /var/log/xray/updater.log (best-effort)

set -eu

LOG_FILE="/var/log/xray/updater.log"
TARGET_CONFIG="/etc/xray/config.json"
DOWNLOAD_FILE="/tmp/subscription.body"
WORK_CONFIG="/tmp/new-config.json"
FINAL_CONFIG="/tmp/new-config.final.json"

log() {
  msg="$(date '+%Y-%m-%d %H:%M:%S') $1"
  echo "$msg"
  mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
  echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

sha256_file() {
  if [ -f "$1" ]; then
    sha256sum "$1" | awk '{print $1}'
  else
    echo ""
  fi
}

require_bin() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log "ERROR Required binary not found: $1"
    exit 1
  fi
}

# Ensure required tools exist in container
require_bin curl
require_bin jq
require_bin python3

if [ -z "${XRAY_SUBSCRIPTION_URL:-}" ]; then
  log "ERROR XRAY_SUBSCRIPTION_URL is not set"
  exit 1
fi

# Download
log "INFO Downloading subscription"
if ! curl -fsSL --connect-timeout 10 --max-time 60 -o "$DOWNLOAD_FILE" "$XRAY_SUBSCRIPTION_URL"; then
  log "ERROR Failed to download subscription"
  exit 1
fi

old_sum="$(sha256_file "$TARGET_CONFIG")"

# Decide mode: full JSON vs. "links" (HTML/text/base64/etc.)
if jq -e '.inbounds and .outbounds' "$DOWNLOAD_FILE" >/dev/null 2>&1; then
  log "INFO Full Xray JSON detected; normalizing inbound ports"

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

else
  log "INFO Not a full Xray JSON; trying to extract links (html/text/base64) and generate Xray config"

  # html2xray.py MUST accept: <input_file> <output_file>
  # It should:
  # - extract vless/vmess/trojan/ss/ssr links from HTML/text
  # - if none found, try base64-decode whole payload and extract again
  # - write a FULL Xray JSON config with inbounds/outbounds to output_file
  if ! python3 /scripts/html2xray.py "$DOWNLOAD_FILE" "$WORK_CONFIG"; then
    log "ERROR Cannot parse subscription as full JSON nor as links; keep current config"
    exit 1
  fi
fi

# Validate generated JSON before replacing config
if ! jq -e '.inbounds and .outbounds' "$WORK_CONFIG" >/dev/null 2>&1; then
  log "ERROR Generated config is not a valid full Xray config (missing inbounds/outbounds); keep current config"
  # Show last bytes for debugging (best-effort, no secrets beyond structure)
  tail -c 200 "$WORK_CONFIG" | tr '\n' ' ' | tr -d '\r' | sed 's/[^[:print:]]/?/g' 1>&2 || true
  exit 1
fi

# Compose final config with local gateway/routing policy
if ! python3 /scripts/compose_xray_config.py "$WORK_CONFIG" "$FINAL_CONFIG"; then
  log "ERROR Failed to compose final config; keep current config"
  exit 1
fi

# Ensure final config is valid
if ! jq -e '.inbounds and .outbounds and .routing' "$FINAL_CONFIG" >/dev/null 2>&1; then
  log "ERROR Final config is invalid (missing inbounds/outbounds/routing); keep current config"
  tail -c 200 "$FINAL_CONFIG" | tr '\n' ' ' | tr -d '\r' | sed 's/[^[:print:]]/?/g' 1>&2 || true
  exit 1
fi

new_sum="$(sha256_file "$FINAL_CONFIG")"
if [ -n "$new_sum" ] && [ "$new_sum" != "$old_sum" ]; then
  # Atomic replace
  mv -f "$FINAL_CONFIG" "$TARGET_CONFIG"
  rm -f "$WORK_CONFIG" 2>/dev/null || true
  log "INFO Config updated (checksum changed)"
else
  rm -f "$FINAL_CONFIG" 2>/dev/null || true
  rm -f "$WORK_CONFIG" 2>/dev/null || true
  log "INFO Config unchanged; no replace"
fi

# Keep a copy of raw payload for troubleshooting
# (best-effort, may fail if /etc/xray is RO in some setups)
cp "$DOWNLOAD_FILE" /etc/xray/subscription.raw 2>/dev/null || true

log "INFO Done"
