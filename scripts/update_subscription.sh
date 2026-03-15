#!/bin/sh
# update_subscription.sh
# - Downloads XRAY_SUBSCRIPTION_URL payload
# - If payload is full Xray JSON (has .inbounds and .outbounds) -> use as source
# - Else (HTML / text / base64 subscription) -> extract links via html2xray.py and generate source Xray config
# - Compose final local config via compose_xray_config.py (gateway/routing/bypass policy)
# - Applies config via single-writer pipeline (lock + validation + atomic replace)
# - Logs to stdout + /var/log/xray/updater.log (best-effort)

set -eu

LOG_FILE="/var/log/xray/updater.log"
TARGET_CONFIG="/etc/xray/config.json"
DOWNLOAD_FILE="/tmp/subscription.body"
WORK_CONFIG="/tmp/new-config.json"
FINAL_CONFIG="/tmp/new-config.final.json"
APPLY_SCRIPT="/scripts/apply_xray_config.py"
RAW_SUBSCRIPTION_DIR="/var/log/xray/raw"
RAW_SUBSCRIPTION_FILE="$RAW_SUBSCRIPTION_DIR/subscription.raw"
SAVE_RAW_SUBSCRIPTION="${XRAY_SAVE_RAW_SUBSCRIPTION:-0}"

log() {
  msg="$(date '+%Y-%m-%d %H:%M:%S') $1"
  echo "$msg"
  mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
  echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
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

if [ ! -f "$APPLY_SCRIPT" ]; then
  log "ERROR Apply pipeline script not found: $APPLY_SCRIPT"
  exit 1
fi

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

# Single-writer apply pipeline (validate + lock + atomic replace)
if ! python3 "$APPLY_SCRIPT" "$FINAL_CONFIG" "$TARGET_CONFIG"; then
  log "ERROR Failed to apply final config; keep current config"
  rm -f "$FINAL_CONFIG" 2>/dev/null || true
  rm -f "$WORK_CONFIG" 2>/dev/null || true
  exit 1
fi

rm -f "$FINAL_CONFIG" 2>/dev/null || true
rm -f "$WORK_CONFIG" 2>/dev/null || true
log "INFO Apply pipeline finished"

# Optional raw payload retention for troubleshooting.
# Disabled by default to avoid leaking provider data into repo-mounted config paths.
if [ "$SAVE_RAW_SUBSCRIPTION" = "1" ]; then
  mkdir -p "$RAW_SUBSCRIPTION_DIR" 2>/dev/null || true
  if cp "$DOWNLOAD_FILE" "$RAW_SUBSCRIPTION_FILE" 2>/dev/null; then
    chmod 600 "$RAW_SUBSCRIPTION_FILE" 2>/dev/null || true
    log "INFO Saved raw subscription payload to $RAW_SUBSCRIPTION_FILE"
  else
    log "WARNING Failed to persist raw subscription payload"
  fi
else
  log "INFO Raw subscription retention is disabled (XRAY_SAVE_RAW_SUBSCRIPTION=0)"
fi

log "INFO Done"
