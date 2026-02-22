#!/bin/sh

set -eu

LOG_DIR="/var/log/xray"

# ensure host-mounted log directory exists and writable
mkdir -p "$LOG_DIR"
chmod 755 "$LOG_DIR"

exec /usr/bin/xray "$@"
