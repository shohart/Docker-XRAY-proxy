#!/bin/sh
set -eu

LAN_CIDR="${LAN_CIDR:-}"
GATEWAY_MODE="${GATEWAY_MODE:-0}"
TPROXY_PORT="${GATEWAY_TPROXY_PORT:-12345}"
BYPASS_IP_CIDRS="${BYPASS_IP_CIDRS:-}"
BYPASS_IP_MASKS="${BYPASS_IP_MASKS:-}"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $1"
}

normalize_mask_to_cidr() {
  mask="$1"
  case "$mask" in
    *\**)
      IFS='.' read -r o1 o2 o3 o4 <<EOF
$mask
EOF
      [ -n "$o1" ] && [ -n "$o2" ] && [ -n "$o3" ] && [ -n "$o4" ] || return 1
      fixed=0
      wildcard_seen=0
      for o in "$o1" "$o2" "$o3" "$o4"; do
        if [ "$o" = "*" ]; then
          wildcard_seen=1
        else
          [ "$wildcard_seen" -eq 0 ] || return 1
          case "$o" in
            ''|*[!0-9]*) return 1 ;;
          esac
          [ "$o" -ge 0 ] && [ "$o" -le 255 ] || return 1
          fixed=$((fixed + 1))
        fi
      done
      prefix=$((fixed * 8))
      [ "$o1" = "*" ] && o1=0
      [ "$o2" = "*" ] && o2=0
      [ "$o3" = "*" ] && o3=0
      [ "$o4" = "*" ] && o4=0
      echo "${o1}.${o2}.${o3}.${o4}/${prefix}"
      ;;
    *)
      echo "$mask"
      ;;
  esac
}

collect_bypass_cidrs() {
  out=""
  merged="$BYPASS_IP_CIDRS,$BYPASS_IP_MASKS"
  OLDIFS="$IFS"
  IFS=','
  for raw in $merged; do
    v="$(echo "$raw" | xargs)"
    if [ -z "$v" ]; then
      continue
    fi
    if ! cidr="$(normalize_mask_to_cidr "$v")"; then
      log "ERROR invalid BYPASS_IP_MASKS/BYPASS_IP_CIDRS entry: $v"
      exit 1
    fi
    if [ -z "$out" ]; then
      out="$cidr"
    else
      out="$out $cidr"
    fi
  done
  IFS="$OLDIFS"
  echo "$out"
}

if [ "$GATEWAY_MODE" != "1" ]; then
  log "INFO GATEWAY_MODE != 1, skip iptables setup"
  exit 0
fi

if [ -z "$LAN_CIDR" ]; then
  log "ERROR LAN_CIDR is required in gateway mode"
  exit 1
fi

BYPASS_CIDRS="$(collect_bypass_cidrs)"

iptables -t nat -N XRAY_GW 2>/dev/null || true
iptables -N XRAY_GW_FWD 2>/dev/null || true

iptables -t nat -F XRAY_GW
iptables -F XRAY_GW_FWD

iptables -t nat -C PREROUTING -s "$LAN_CIDR" -p tcp -j XRAY_GW 2>/dev/null || \
  iptables -t nat -I PREROUTING 1 -s "$LAN_CIDR" -p tcp -j XRAY_GW

iptables -C FORWARD -s "$LAN_CIDR" -j XRAY_GW_FWD 2>/dev/null || \
  iptables -I FORWARD 1 -s "$LAN_CIDR" -j XRAY_GW_FWD

# Never proxy local networks and loopback/broadcast traffic.
for ipr in "$LAN_CIDR" 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.0/8 169.254.0.0/16 224.0.0.0/4 255.255.255.255/32; do
  iptables -t nat -A XRAY_GW -d "$ipr" -j RETURN
done

for ipr in $BYPASS_CIDRS; do
  iptables -t nat -A XRAY_GW -d "$ipr" -j RETURN
done

# Transparent redirect of TCP traffic to Xray dokodemo-door inbound.
iptables -t nat -A XRAY_GW -p tcp -j REDIRECT --to-ports "$TPROXY_PORT"

# Fail-closed for forwarded LAN traffic:
# allow only LAN destinations or explicit bypass CIDRs, reject the rest.
iptables -A XRAY_GW_FWD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A XRAY_GW_FWD -d "$LAN_CIDR" -j ACCEPT
for ipr in $BYPASS_CIDRS; do
  iptables -A XRAY_GW_FWD -d "$ipr" -j ACCEPT
done
iptables -A XRAY_GW_FWD -j REJECT

log "INFO Gateway iptables rules applied"
