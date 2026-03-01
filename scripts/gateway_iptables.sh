#!/bin/sh
set -eu

LAN_CIDR="${LAN_CIDR:-}"
GATEWAY_MODE="${GATEWAY_MODE:-0}"
TPROXY_PORT="${GATEWAY_TPROXY_PORT:-12345}"
BYPASS_MARK="${GATEWAY_FWMARK:-1}"
BYPASS_TABLE="${GATEWAY_ROUTE_TABLE:-100}"
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

iptables -t mangle -N XRAY_GW 2>/dev/null || true
iptables -N XRAY_GW_FWD 2>/dev/null || true

iptables -t mangle -F XRAY_GW
iptables -F XRAY_GW_FWD

iptables -t mangle -C PREROUTING -s "$LAN_CIDR" -j XRAY_GW 2>/dev/null || \
  iptables -t mangle -I PREROUTING 1 -s "$LAN_CIDR" -j XRAY_GW

iptables -C FORWARD -s "$LAN_CIDR" -j XRAY_GW_FWD 2>/dev/null || \
  iptables -I FORWARD 1 -s "$LAN_CIDR" -j XRAY_GW_FWD

# Never proxy local networks and loopback/broadcast traffic.
for ipr in "$LAN_CIDR" 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.0/8 169.254.0.0/16 224.0.0.0/4 255.255.255.255/32; do
  iptables -t mangle -A XRAY_GW -d "$ipr" -j RETURN
done

for ipr in $BYPASS_CIDRS; do
  iptables -t mangle -A XRAY_GW -d "$ipr" -j RETURN
done

# Transparent interception to Xray dokodemo-door inbound (TCP + UDP).
iptables -t mangle -A XRAY_GW -p tcp -j TPROXY --on-port "$TPROXY_PORT" --tproxy-mark "${BYPASS_MARK}/${BYPASS_MARK}"
iptables -t mangle -A XRAY_GW -p udp -j TPROXY --on-port "$TPROXY_PORT" --tproxy-mark "${BYPASS_MARK}/${BYPASS_MARK}"

# Route marked packets locally so Xray can accept original destination.
ip rule del fwmark "$BYPASS_MARK" table "$BYPASS_TABLE" 2>/dev/null || true
ip rule add fwmark "$BYPASS_MARK" table "$BYPASS_TABLE"
ip route flush table "$BYPASS_TABLE" 2>/dev/null || true
ip route add local 0.0.0.0/0 dev lo table "$BYPASS_TABLE"

# Fail-closed for forwarded LAN traffic:
# allow only LAN destinations or explicit bypass CIDRs, reject the rest.
iptables -A XRAY_GW_FWD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A XRAY_GW_FWD -d "$LAN_CIDR" -j ACCEPT
for ipr in $BYPASS_CIDRS; do
  iptables -A XRAY_GW_FWD -d "$ipr" -j ACCEPT
done
iptables -A XRAY_GW_FWD -j REJECT

log "INFO Gateway iptables rules applied"
