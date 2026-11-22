#!/bin/sh
set -eu

LAN_CIDR="${LAN_CIDR:-}"
GATEWAY_MODE="${GATEWAY_MODE:-0}"
TPROXY_PORT="${GATEWAY_TPROXY_PORT:-12345}"
BYPASS_MARK="${GATEWAY_FWMARK:-1}"
BYPASS_TABLE="${GATEWAY_ROUTE_TABLE:-100}"
BYPASS_IP_CIDRS="${BYPASS_IP_CIDRS:-}"
BYPASS_IP_MASKS="${BYPASS_IP_MASKS:-}"

# DNS and DPI bypass options (learned from zapret-proxy stack)
GATEWAY_DNS_SERVER="${GATEWAY_DNS_SERVER:-192.168.1.123}"
GATEWAY_REDIRECT_DNS="${GATEWAY_REDIRECT_DNS:-1}"
GATEWAY_BLOCK_QUIC="${GATEWAY_BLOCK_QUIC:-1}"

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

cleanup() {
  log "INFO Caught shutdown signal or cleanup requested; tearing down gateway firewall rules..."

  # Remove PREROUTING hooks
  if [ -n "$LAN_CIDR" ]; then
    iptables -t mangle -D PREROUTING -s "$LAN_CIDR" -j XRAY_GW 2>/dev/null || true
    iptables -D FORWARD -s "$LAN_CIDR" -j XRAY_GW_FWD 2>/dev/null || true
  fi

  # Remove DNS DNAT and Hairpin SNAT rules
  if [ -n "$LAN_CIDR" ] && [ -n "$GATEWAY_DNS_SERVER" ]; then
    iptables -t nat -D PREROUTING -s "$LAN_CIDR" -p udp --dport 53 -j DNAT --to-destination "${GATEWAY_DNS_SERVER}:53" 2>/dev/null || true
    iptables -t nat -D PREROUTING -s "$LAN_CIDR" -p tcp --dport 53 -j DNAT --to-destination "${GATEWAY_DNS_SERVER}:53" 2>/dev/null || true
    iptables -t nat -D POSTROUTING -s "$LAN_CIDR" -d "$GATEWAY_DNS_SERVER" -p udp --dport 53 -j MASQUERADE 2>/dev/null || true
    iptables -t nat -D POSTROUTING -s "$LAN_CIDR" -d "$GATEWAY_DNS_SERVER" -p tcp --dport 53 -j MASQUERADE 2>/dev/null || true
  fi

  # Remove direct LAN outbound masquerade
  if [ -n "$LAN_CIDR" ]; then
    iptables -t nat -D POSTROUTING -s "$LAN_CIDR" ! -d "$LAN_CIDR" -j MASQUERADE 2>/dev/null || true
  fi

  # Flush and delete custom chains
  iptables -t mangle -F XRAY_GW 2>/dev/null || true
  iptables -t mangle -X XRAY_GW 2>/dev/null || true
  iptables -F XRAY_GW_FWD 2>/dev/null || true
  iptables -X XRAY_GW_FWD 2>/dev/null || true

  # Delete policy routing rule and flush route table
  while ip rule del fwmark "$BYPASS_MARK" table "$BYPASS_TABLE" 2>/dev/null; do :; done
  ip route flush table "$BYPASS_TABLE" 2>/dev/null || true

  log "INFO Gateway cleanup finished"
  exit 0
}

trap cleanup SIGTERM SIGINT SIGHUP

if [ "$GATEWAY_MODE" != "1" ]; then
  log "INFO GATEWAY_MODE != 1, cleaning any existing rules and skipping gateway setup"
  cleanup
fi

if [ -z "$LAN_CIDR" ]; then
  log "ERROR LAN_CIDR is required in gateway mode"
  exit 1
fi

BYPASS_CIDRS="$(collect_bypass_cidrs)"

# Create custom chains if not exist, and always flush to guarantee idempotency
iptables -t mangle -N XRAY_GW 2>/dev/null || true
iptables -N XRAY_GW_FWD 2>/dev/null || true

iptables -t mangle -F XRAY_GW
iptables -F XRAY_GW_FWD

# Hook into PREROUTING and FORWARD
iptables -t mangle -C PREROUTING -s "$LAN_CIDR" -j XRAY_GW 2>/dev/null || \
  iptables -t mangle -I PREROUTING 1 -s "$LAN_CIDR" -j XRAY_GW

iptables -C FORWARD -s "$LAN_CIDR" -j XRAY_GW_FWD 2>/dev/null || \
  iptables -I FORWARD 1 -s "$LAN_CIDR" -j XRAY_GW_FWD

# --- Mangle Table (XRAY_GW): Transparent proxy interception ---
# 1. Never proxy local networks, loopback, multicast and broadcast
for ipr in "$LAN_CIDR" 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.0/8 169.254.0.0/16 224.0.0.0/4 255.255.255.255/32; do
  iptables -t mangle -A XRAY_GW -d "$ipr" -j RETURN
done

# 2. Never proxy explicit bypass CIDRs / masks
for ipr in $BYPASS_CIDRS; do
  iptables -t mangle -A XRAY_GW -d "$ipr" -j RETURN
done

# 3. DNS queries: do not TPROXY, let NAT redirection handle them cleanly
iptables -t mangle -A XRAY_GW -p udp --dport 53 -j RETURN
iptables -t mangle -A XRAY_GW -p tcp --dport 53 -j RETURN

# 4. QUIC (UDP 443): drop in mangle (or reject in FORWARD) to force instant HTTP/2 TCP fallback (prevents stalls over VLESS/DPI)
if [ "$GATEWAY_BLOCK_QUIC" = "1" ]; then
  iptables -t mangle -A XRAY_GW -p udp --dport 443 -j DROP
fi

# 5. Transparent interception to Xray dokodemo-door inbound (TCP + UDP)
iptables -t mangle -A XRAY_GW -p tcp -j TPROXY --on-port "$TPROXY_PORT" --tproxy-mark "${BYPASS_MARK}/${BYPASS_MARK}"
iptables -t mangle -A XRAY_GW -p udp -j TPROXY --on-port "$TPROXY_PORT" --tproxy-mark "${BYPASS_MARK}/${BYPASS_MARK}"

# Route marked packets locally so Xray can accept original destination
while ip rule del fwmark "$BYPASS_MARK" table "$BYPASS_TABLE" 2>/dev/null; do :; done
ip rule add fwmark "$BYPASS_MARK" table "$BYPASS_TABLE"
ip route flush table "$BYPASS_TABLE" 2>/dev/null || true
ip route add local 0.0.0.0/0 dev lo table "$BYPASS_TABLE"

# --- NAT Table: DNS redirection & Hairpin SNAT ---
if [ "$GATEWAY_REDIRECT_DNS" = "1" ] && [ -n "$GATEWAY_DNS_SERVER" ]; then
  # Transparently redirect all DNS queries from LAN to trusted DNS server (Technitium)
  iptables -t nat -C PREROUTING -s "$LAN_CIDR" -p udp --dport 53 -j DNAT --to-destination "${GATEWAY_DNS_SERVER}:53" 2>/dev/null || \
    iptables -t nat -I PREROUTING 1 -s "$LAN_CIDR" -p udp --dport 53 -j DNAT --to-destination "${GATEWAY_DNS_SERVER}:53"

  iptables -t nat -C PREROUTING -s "$LAN_CIDR" -p tcp --dport 53 -j DNAT --to-destination "${GATEWAY_DNS_SERVER}:53" 2>/dev/null || \
    iptables -t nat -I PREROUTING 1 -s "$LAN_CIDR" -p tcp --dport 53 -j DNAT --to-destination "${GATEWAY_DNS_SERVER}:53"

  # Hairpin NAT: if DNS server is on same LAN subnet, MASQUERADE so replies return via gateway
  iptables -t nat -C POSTROUTING -s "$LAN_CIDR" -d "$GATEWAY_DNS_SERVER" -p udp --dport 53 -j MASQUERADE 2>/dev/null || \
    iptables -t nat -A POSTROUTING -s "$LAN_CIDR" -d "$GATEWAY_DNS_SERVER" -p udp --dport 53 -j MASQUERADE

  iptables -t nat -C POSTROUTING -s "$LAN_CIDR" -d "$GATEWAY_DNS_SERVER" -p tcp --dport 53 -j MASQUERADE 2>/dev/null || \
    iptables -t nat -A POSTROUTING -s "$LAN_CIDR" -d "$GATEWAY_DNS_SERVER" -p tcp --dport 53 -j MASQUERADE
fi

# MASQUERADE direct forwarded LAN traffic going to external networks (e.g. bypass CIDRs)
iptables -t nat -C POSTROUTING -s "$LAN_CIDR" ! -d "$LAN_CIDR" -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -s "$LAN_CIDR" ! -d "$LAN_CIDR" -j MASQUERADE

# --- Filter Table (XRAY_GW_FWD): Fail-closed forwarding policy ---
iptables -A XRAY_GW_FWD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A XRAY_GW_FWD -d "$LAN_CIDR" -j ACCEPT

# Allow forwarding to DNS server
if [ -n "$GATEWAY_DNS_SERVER" ]; then
  iptables -A XRAY_GW_FWD -d "$GATEWAY_DNS_SERVER" -p udp --dport 53 -j ACCEPT
  iptables -A XRAY_GW_FWD -d "$GATEWAY_DNS_SERVER" -p tcp --dport 53 -j ACCEPT
fi

# Allow LAN clients to access ALL Docker bridge containers on this host without manual port enumerations
iptables -A XRAY_GW_FWD -o docker0 -j ACCEPT
iptables -A XRAY_GW_FWD -o br-+ -j ACCEPT
for docker_subnet in 172.16.0.0/12 192.168.16.0/20 192.168.32.0/20 192.168.48.0/20 192.168.64.0/20 192.168.80.0/20 192.168.96.0/20 192.168.112.0/20; do
  iptables -A XRAY_GW_FWD -d "$docker_subnet" -j ACCEPT
done

# Allow explicit bypass CIDRs
for ipr in $BYPASS_CIDRS; do
  iptables -A XRAY_GW_FWD -d "$ipr" -j ACCEPT
done

# Fail-closed: block direct forwarded internet traffic
iptables -A XRAY_GW_FWD -j REJECT --reject-with icmp-port-unreachable

# Host safety: preserve SSH and Mosh access on custom ports in INPUT
iptables -C INPUT -p tcp --dport 2229 -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport 2229 -j ACCEPT
iptables -C INPUT -p udp --dport 2220:2235 -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p udp --dport 2220:2235 -j ACCEPT
iptables -C INPUT -p udp --dport 60000:61000 -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p udp --dport 60000:61000 -j ACCEPT

log "INFO Gateway iptables rules applied successfully. Supervisor running..."

# Signal-aware wait loop so SIGTERM triggers trap immediately
while :; do
  sleep 3600 &
  wait $!
done
