#!/bin/sh
set -eu

CONFIG="/etc/xray/config.json"
LOG="/var/log/xray/xray-watch.log"

log() {
    msg="$(date '+%Y-%m-%d %H:%M:%S') $1"
    echo "$msg"
    mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
    echo "$msg" >> "$LOG" 2>/dev/null || true
}

sha256_file() {
    [ -f "$1" ] && sha256sum "$1" | awk '{print $1}' || echo ""
}

start_xray() {
    log "INFO starting xray"
    xray run -c "$CONFIG" &
    XRAY_PID=$!
    log "INFO xray pid=$XRAY_PID"
}

stop_xray() {
    if [ -n "${XRAY_PID:-}" ] && kill -0 "$XRAY_PID" 2>/dev/null; then
        log "INFO stopping xray pid=$XRAY_PID"
        kill -TERM "$XRAY_PID" 2>/dev/null || true
        # wait up to 15s
        for i in $(seq 1 15); do
            if kill -0 "$XRAY_PID" 2>/dev/null; then
                sleep 1
            else
                break
            fi
        done
        # force if still alive
        if kill -0 "$XRAY_PID" 2>/dev/null; then
            log "WARN xray did not stop gracefully; killing"
            kill -KILL "$XRAY_PID" 2>/dev/null || true
        fi
    fi
}

trap 'stop_xray; exit 0' INT TERM

# Wait until config exists
while [ ! -s "$CONFIG" ]; do
    log "WARN $CONFIG not found or empty; waiting..."
    sleep 2
done

last_sum="$(sha256_file "$CONFIG")"
log "INFO initial config sha256=${last_sum:-<empty>}"

start_xray

while true; do
    sleep 3
    cur_sum="$(sha256_file "$CONFIG")"
    
    # If xray died, restart
    if ! kill -0 "$XRAY_PID" 2>/dev/null; then
        log "WARN xray process exited; restarting"
        start_xray
        last_sum="$cur_sum"
        continue
    fi
    
    if [ -n "$cur_sum" ] && [ "$cur_sum" != "$last_sum" ]; then
        log "INFO config changed sha256=$last_sum -> $cur_sum; restarting xray"
        stop_xray
        last_sum="$cur_sum"
        start_xray
    fi
done