#!/bin/sh
# update_subscription.sh
# Downloads ALL subscription URLs, parses each through sub2xray.py,
# merges VLESS outbounds, composes final config via compose_xray_config.py.
#
# Sources (all optional except primary):
#   XRAY_SUBSCRIPTION_URL     — primary (required)
#   GHOSTLINE_SUBSCRIPTION_URL — GhostLine (optional, backward compat)
#   EXTRA_SUBSCRIPTION_URLS   — comma-separated list of additional URLs
#
# All outbounds from all sources are merged with provider-prefixed tags
# (node1, node2, ...) into a single source config, then composed with
# gateway/routing/balancer/openvpn-fallback policy.
#
# Resilience (a single provider hiccup must not shrink the node pool):
#   - every successfully parsed source is cached in $CACHE_DIR; when a
#     download/parse fails, the last good copy of that source is reused
#     (up to SUB_CACHE_MAX_AGE_H hours old);
#   - if the primary source failed with no usable cache and the new config
#     would have fewer VLESS nodes than the current one, the current config
#     is kept. (Secondary sources are not guarded: some are dead for good and
#     would otherwise block legitimate node removals forever.)

set -eu

LOG_FILE="/var/log/xray/updater.log"
TARGET_CONFIG="/etc/xray/config.json"
FINAL_CONFIG="/tmp/new-config.final.json"
CACHE_DIR="/etc/xray/sub-cache"
CACHE_MAX_AGE_H="${SUB_CACHE_MAX_AGE_H:-168}"

log() {
  msg="$(date '+%Y-%m-%d %H:%M:%S') $1"
  echo "$msg"
  mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
  # keep updater.log bounded (~1 MiB) so it can't grow without limit
  if [ -f "$LOG_FILE" ] && [ "$(wc -c <"$LOG_FILE" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    tail -c 262144 "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE" 2>/dev/null || true
  fi
  echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

sha256_file() {
  if [ -f "$1" ]; then sha256sum "$1" | awk '{print $1}'; else echo ""; fi
}

require_bin() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log "ERROR Required binary not found: $1"
    exit 1
  fi
}

require_bin curl
require_bin jq
require_bin python3

if [ -z "${XRAY_SUBSCRIPTION_URL:-}" ]; then
  log "ERROR XRAY_SUBSCRIPTION_URL is not set"
  exit 1
fi

# --- Collect all subscription URLs ---
ALL_URLS="$XRAY_SUBSCRIPTION_URL"

# Backward compat: GHOSTLINE_SUBSCRIPTION_URL
if [ -n "${GHOSTLINE_SUBSCRIPTION_URL:-}" ]; then
  ALL_URLS="$ALL_URLS,$GHOSTLINE_SUBSCRIPTION_URL"
fi

# Extra URLs (comma-separated)
if [ -n "${EXTRA_SUBSCRIPTION_URLS:-}" ]; then
  ALL_URLS="$ALL_URLS,$EXTRA_SUBSCRIPTION_URLS"
fi

# --- Download and parse a single subscription URL ---
# Args: $1=url  $2=output_json_path
#
# URL prefix `happ+` (e.g. happ+https://provider/sub/token): fetch with the
# request profile of the Happ Android client (User-agent + X-HWID + device
# headers). Providers gated on INCY/Happ-format subscriptions (Legenda etc.)
# serve real configs only to app-like clients; everything else gets
# placeholder 0.0.0.0 nodes. The HWID is stable so the updater occupies one
# device slot instead of burning a new one on every refresh.
HAPP_HWID="${HAPP_HWID:-d4f1890afd458778}"
HAPP_UA="Happ/4.7.1/Android/17912103176801703650"

download_and_parse() {
  local url="$1"
  local out="$2"
  local real_url="$url"
  local extra_args=""
  local label="$url"

  case "$url" in
    happ+*)
      real_url="${url#happ+}"
      label="$real_url"
      extra_args="-H User-agent:$HAPP_UA -H X-Device-Locale:ru -H X-HWID:$HAPP_HWID -H X-Device-OS:Android -H X-Ver-OS:16 -H X-Device-model:SM-S928B"
      ;;
  esac

  local raw="/tmp/sub_$(echo "$url" | md5sum | cut -c1-8).body"

  log "INFO Fetching ${label%%/*}..."
  if ! curl -fsSL --connect-timeout 10 --max-time 60 $extra_args -o "$raw" "$real_url"; then
    log "WARN Download failed: ${label%%/*}"
    return 1
  fi

  if ! python3 /scripts/sub2xray.py "$raw" "$out"; then
    log "WARN sub2xray.py failed for ${label%%/*}"
    return 1
  fi

  # Reject placeholder-only responses (provider gating / device-limit message):
  # sub2xray.py exits 1 when every node is a 0.0.0.0 dummy, so this also
  # prevents caching an all-dummy source.
  local count
  count=$(jq '[.outbounds[]? | select(.protocol=="vless")] | length' "$out" 2>/dev/null || echo 0)
  if [ "$count" -eq 0 ]; then
    log "WARN Source ${label%%/*} parsed to 0 VLESS nodes; skipping"
    return 1
  fi

  rm -f "$raw" 2>/dev/null || true
  return 0
}

# Count VLESS outbounds in an Xray config (0 if missing/invalid)
vless_count() {
  if [ -f "$1" ]; then
    jq '[.outbounds[]? | select(.protocol=="vless")] | length' "$1" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

# Cache path is keyed by URL hash, so reordering sources doesn't mix caches
cache_path() {
  echo "$CACHE_DIR/src_$(echo "$1" | md5sum | cut -c1-16).json"
}

# Reuse the last good parsed copy of a source if it is fresh enough.
# Args: $1=url  $2=output_json_path
use_cached_source() {
  local cached
  cached="$(cache_path "$1")"
  [ -s "$cached" ] || return 1
  local age_h=$(( ($(date +%s) - $(date -r "$cached" +%s)) / 3600 ))
  if [ "$age_h" -gt "$CACHE_MAX_AGE_H" ]; then
    log "WARN Cache for ${1%%/*} is ${age_h}h old (> ${CACHE_MAX_AGE_H}h); not used"
    return 1
  fi
  cp "$cached" "$2" || return 1
  log "WARN Using cached copy (${age_h}h old) for failed source"
  return 0
}

# --- Main ---
old_sum="$(sha256_file "$TARGET_CONFIG")"
mkdir -p "$CACHE_DIR" 2>/dev/null || true

# Parse each URL, collect successful source configs
SOURCES=""
IDX=0
TOTAL=0
FAILED=0
CACHED=0
# Primary source failed and had no usable cache: the pool is really shrinking
PRIMARY_LOST=0

# Save IFS, split on comma
OLD_IFS="$IFS"
IFS=','
for url in $ALL_URLS; do
  IFS="$OLD_IFS"
  url="$(echo "$url" | xargs)"  # trim whitespace
  [ -z "$url" ] && continue
  IDX=$((IDX + 1))
  TOTAL=$((TOTAL + 1))
  SRC="/tmp/sub_src_${IDX}.json"

  if download_and_parse "$url" "$SRC"; then
    # Count vless outbounds in this source
    COUNT=$(jq '[.outbounds[] | select(.protocol=="vless")] | length' "$SRC" 2>/dev/null || echo 0)
    log "OK Source $IDX: $COUNT VLESS nodes"
    SOURCES="$SOURCES $SRC"
    if [ "$COUNT" -gt 0 ]; then
      cp "$SRC" "$(cache_path "$url")" 2>/dev/null || true
    fi
  else
    FAILED=$((FAILED + 1))
    rm -f "$SRC" 2>/dev/null || true
    if use_cached_source "$url" "$SRC"; then
      CACHED=$((CACHED + 1))
      COUNT=$(jq '[.outbounds[] | select(.protocol=="vless")] | length' "$SRC" 2>/dev/null || echo 0)
      log "OK Source $IDX: $COUNT VLESS nodes (cached)"
      SOURCES="$SOURCES $SRC"
    elif [ "$IDX" -eq 1 ]; then
      PRIMARY_LOST=1
    fi
  fi
done
IFS="$OLD_IFS"

# Trim leading space
SOURCES="$(echo "$SOURCES" | xargs)"

if [ -z "$SOURCES" ]; then
  log "ERROR All subscriptions failed ($FAILED/$TOTAL); keep current config"
  exit 1
fi

log "INFO $((TOTAL - FAILED))/$TOTAL sources OK ($CACHED from cache), merging..."

# --- Merge all sources into one config ---
MERGED="/tmp/sub_merged.json"
python3 -c "
import json, sys

sources = sys.argv[1:]
all_outbounds = []
seen_tags = set()
seen_keys = set()  # (address, port, id) for dedup

for src_path in sources:
    try:
        cfg = json.load(open(src_path))
    except Exception:
        continue
    for ob in cfg.get('outbounds', []):
        proto = ob.get('protocol', '')
        if proto in ('freedom', 'blackhole'):
            continue
        tag = ob.get('tag', '')
        # Dedup by (address, port, id)
        vnext = ob.get('settings', {}).get('vnext', [{}])
        if vnext:
            addr = vnext[0].get('address', '')
            port = vnext[0].get('port', 0)
            uid = vnext[0].get('users', [{}])[0].get('id', '')
            key = (addr, port, uid)
            if key in seen_keys:
                continue
            seen_keys.add(key)
        if tag in seen_tags:
            # Tag collision (e.g. node1 from two providers) — rename
            # the later one with a numeric suffix so both survive.
            base_tag = tag
            suffix = 2
            while base_tag + '_' + str(suffix) in seen_tags:
                suffix += 1
            tag = base_tag + '_' + str(suffix)
            ob['tag'] = tag
        seen_tags.add(tag)
        all_outbounds.append(ob)

if not all_outbounds:
    print('ERROR: no VLESS outbounds after merge', file=sys.stderr)
    sys.exit(1)

# Build source config
http_port = 3128
socks_port = 1080
merged = {
    'log': {'loglevel': 'info'},
    'inbounds': [
        {'port': http_port, 'protocol': 'http', 'settings': {'users': []},
         'sniffing': {'enabled': True, 'destOverride': ['http', 'tls']}},
        {'port': socks_port, 'protocol': 'socks', 'settings': {'auth': 'noauth', 'udp': True},
         'sniffing': {'enabled': True, 'destOverride': ['http', 'tls']}},
    ],
    'outbounds': all_outbounds + [
        {'tag': 'direct', 'protocol': 'freedom', 'settings': {}},
        {'tag': 'block', 'protocol': 'blackhole', 'settings': {}},
    ],
    'routing': {
        'domainStrategy': 'IPOnDemand',
        'rules': [{'type': 'field', 'ip': ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'],
                    'outboundTag': 'direct'}],
    },
}

json.dump(merged, open('$MERGED', 'w'), ensure_ascii=False, indent=2)
print(f'[OK] merged {len(all_outbounds)} VLESS outbounds from {len(sources)} sources')
" $SOURCES

SOURCE="$MERGED"

# Validate source config
if ! jq -e '.outbounds' "$SOURCE" >/dev/null 2>&1; then
  log "ERROR Source config has no outbounds"
  exit 1
fi

# Compose final config with local gateway/routing/balancer/ovpn policy
if ! python3 /scripts/compose_xray_config.py "$SOURCE" "$FINAL_CONFIG"; then
  log "ERROR Failed to compose final config; keep current config"
  exit 1
fi

# Validate final config
if ! jq -e '.inbounds and .outbounds and .routing' "$FINAL_CONFIG" >/dev/null 2>&1; then
  log "ERROR Final config is invalid; keep current config"
  exit 1
fi

# Regression guard: a failed primary source (with no cache) must not
# silently shrink the node pool of a working config.
old_nodes="$(vless_count "$TARGET_CONFIG")"
new_nodes="$(vless_count "$FINAL_CONFIG")"
if [ "$PRIMARY_LOST" -eq 1 ] && [ "$new_nodes" -lt "$old_nodes" ]; then
  rm -f "$FINAL_CONFIG" /tmp/sub_src_*.json /tmp/sub_merged.json 2>/dev/null || true
  log "WARN Primary source failed without cache and node count would drop $old_nodes -> $new_nodes; keep current config"
  exit 1
fi

# Atomic replace if changed
new_sum="$(sha256_file "$FINAL_CONFIG")"
if [ -n "$new_sum" ] && [ "$new_sum" != "$old_sum" ]; then
  mv -f "$FINAL_CONFIG" "$TARGET_CONFIG"
  log "INFO Config updated (checksum changed)"
else
  rm -f "$FINAL_CONFIG" 2>/dev/null || true
  log "INFO Config unchanged; no replace"
fi

# Cleanup
rm -f /tmp/sub_src_*.json /tmp/sub_merged.json 2>/dev/null || true
cp "$TARGET_CONFIG" /etc/xray/config.last.json 2>/dev/null || true

log "INFO Done ($((TOTAL - FAILED))/$TOTAL sources, $FAILED failed, $CACHED from cache, $new_nodes nodes)"
