#!/usr/bin/env python3
import ipaddress
import json
import os
import sys
import urllib.parse


LOCAL_IP_RANGES = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
]


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def parse_csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    items = []
    for part in raw.split(","):
        val = part.strip()
        if val:
            items.append(val)
    return items


def normalize_domain_exact(value: str) -> str:
    d = extract_hostname(value).lower().rstrip(".")
    if not d:
        return ""
    return f"full:{d}"


def normalize_domain_suffix(value: str) -> str:
    d = extract_hostname(value).lower().lstrip(".").rstrip(".")
    if not d:
        return ""
    return f"domain:{d}"


def extract_hostname(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        return parsed.hostname or ""
    if "/" in raw:
        raw = raw.split("/", 1)[0]
    if ":" in raw and raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if port.isdigit():
            raw = host
    if raw.startswith("*."):
        raw = raw[2:]
    return raw


def wildcard_to_cidr(value: str) -> str:
    candidate = value.strip()
    if "*" not in candidate:
        return candidate

    parts = candidate.split(".")
    if len(parts) != 4:
        raise ValueError(f"Invalid wildcard IPv4 mask: {value}")

    fixed = 0
    octets = []
    wildcard_seen = False
    for p in parts:
        if p == "*":
            wildcard_seen = True
            octets.append(0)
            continue
        if wildcard_seen:
            raise ValueError(
                f"Wildcard IPv4 mask must use trailing '*' only: {value}"
            )
        octet = int(p)
        if octet < 0 or octet > 255:
            raise ValueError(f"Invalid octet in wildcard IPv4 mask: {value}")
        fixed += 1
        octets.append(octet)

    prefix = fixed * 8
    return f"{octets[0]}.{octets[1]}.{octets[2]}.{octets[3]}/{prefix}"


def parse_ip_ranges() -> list[str]:
    cidrs = []
    for value in parse_csv_env("BYPASS_IP_CIDRS"):
        cidr = wildcard_to_cidr(value)
        ipaddress.ip_network(cidr, strict=False)
        cidrs.append(cidr)

    for value in parse_csv_env("BYPASS_IP_MASKS"):
        cidr = wildcard_to_cidr(value)
        ipaddress.ip_network(cidr, strict=False)
        cidrs.append(cidr)

    uniq = []
    seen = set()
    for c in cidrs:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def ensure_direct_block(outbounds: list[dict]) -> list[dict]:
    direct = None
    block = None
    result = []

    for outbound in outbounds:
        tag = outbound.get("tag")
        if tag == "direct":
            if direct is None:
                direct = outbound
            continue
        if tag == "block":
            if block is None:
                block = outbound
            continue
        result.append(outbound)

    if direct is None:
        direct = {"tag": "direct", "protocol": "freedom", "settings": {}}
    if block is None:
        block = {"tag": "block", "protocol": "blackhole", "settings": {}}

    result.append(direct)
    result.append(block)
    return result


def reorder_outbounds(outbounds: list[dict]) -> list[dict]:
    proxy = []
    direct = []
    block = []
    other = []
    for o in outbounds:
        tag = o.get("tag")
        proto = o.get("protocol")
        if tag == "direct" or proto == "freedom":
            direct.append(o)
        elif tag == "block" or proto == "blackhole":
            block.append(o)
        elif proto == "dns":
            other.append(o)
        else:
            proxy.append(o)

    if not proxy:
        raise ValueError("No proxy outbounds found in source config")
    return proxy + other + direct + block


def build_inbounds() -> list[dict]:
    http_port = int(os.getenv("HTTP_PROXY_PORT", "3128"))
    socks_port = int(os.getenv("SOCKS_PROXY_PORT", "1080"))
    gateway_mode = os.getenv("GATEWAY_MODE", "0") == "1"
    tproxy_port = int(os.getenv("GATEWAY_TPROXY_PORT", "12345"))

    inbounds = [
        {
            "port": http_port,
            "protocol": "http",
            "settings": {"users": []},
            "sniffing": {
                "enabled": True,
                "destOverride": ["http", "tls", "quic"],
                "routeOnly": True,
            },
        },
        {
            "port": socks_port,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": True},
            "sniffing": {
                "enabled": True,
                "destOverride": ["http", "tls", "quic"],
                "routeOnly": True,
            },
        },
    ]

    if gateway_mode:
        inbounds.append(
            {
                "tag": "tproxy-in",
                "port": tproxy_port,
                "protocol": "dokodemo-door",
                "settings": {
                    "network": "tcp,udp",
                    "followRedirect": True,
                },
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                    "routeOnly": True,
                },
                "streamSettings": {"sockopt": {"tproxy": "tproxy"}},
            }
        )
    return inbounds


def extract_proxy_tags(outbounds: list[dict]) -> list[str]:
    tags = []
    for outbound in outbounds:
        proto = outbound.get("protocol")
        tag = outbound.get("tag")
        if not tag:
            continue
        if tag in ("direct", "block"):
            continue
        if proto in ("freedom", "blackhole", "dns"):
            continue
        tags.append(tag)
    if not tags:
        raise ValueError("No proxy outbound tags available for routing")
    return tags


def build_routing(proxy_tags: list[str]) -> dict:
    exact_domains = parse_csv_env("BYPASS_DOMAINS")
    zone_domains = parse_csv_env("BYPASS_DOMAIN_ZONES")
    bypass_ips = parse_ip_ranges()

    domain_items = []
    for d in exact_domains:
        normalized = normalize_domain_exact(d)
        if normalized:
            domain_items.append(normalized)
    for d in zone_domains:
        normalized = normalize_domain_suffix(d)
        if normalized:
            domain_items.append(normalized)

    ip_items = LOCAL_IP_RANGES + bypass_ips

    rules = []
    if domain_items:
        rules.append(
            {
                "type": "field",
                "domain": domain_items,
                "outboundTag": "direct",
            }
        )
    if ip_items:
        rules.append(
            {
                "type": "field",
                "ip": ip_items,
                "outboundTag": "direct",
            }
        )

    # Default route for all other traffic through proxy path:
    # - single proxy tag -> direct outboundTag to that node
    # - multi-proxy -> use balancer for automatic failover/load spread
    if len(proxy_tags) == 1:
        rules.append(
            {
                "type": "field",
                "network": "tcp,udp",
                "outboundTag": proxy_tags[0],
            }
        )
    else:
        rules.append(
            {
                "type": "field",
                "network": "tcp,udp",
                "balancerTag": "proxy-auto",
            }
        )

    return {"domainStrategy": "IPOnDemand", "rules": rules}


def build_balancer(proxy_tags: list[str]) -> dict:
    strategy = os.getenv("XRAY_BALANCER_STRATEGY", "random").strip()
    if strategy not in ("random", "roundRobin", "leastPing", "leastLoad"):
        raise ValueError(
            "XRAY_BALANCER_STRATEGY must be one of: random, roundRobin, leastPing, leastLoad"
        )

    balancer = {
        "tag": "proxy-auto",
        "selector": proxy_tags,
        # Fail-closed by default: if all proxy nodes are down, do not leak traffic directly.
        "fallbackTag": os.getenv("XRAY_BALANCER_FALLBACK_TAG", "block").strip() or "block",
        "strategy": {"type": strategy},
    }

    if strategy == "leastLoad":
        settings = {}
        expected = os.getenv("XRAY_BALANCER_EXPECTED")
        max_rtt = os.getenv("XRAY_BALANCER_MAX_RTT")
        tolerance = os.getenv("XRAY_BALANCER_TOLERANCE")
        baselines = parse_csv_env("XRAY_BALANCER_BASELINES")
        costs = parse_csv_env("XRAY_BALANCER_COSTS")
        if expected:
            settings["expected"] = int(expected)
        if max_rtt:
            settings["maxRTT"] = max_rtt
        if tolerance:
            settings["tolerance"] = int(tolerance)
        if baselines:
            settings["baselines"] = baselines
        if costs:
            settings["costs"] = costs
        if settings:
            balancer["strategy"]["settings"] = settings
    return balancer


def build_observatory(proxy_tags: list[str]) -> dict:
    return {
        "subjectSelector": proxy_tags,
        "probeUrl": os.getenv(
            "XRAY_PROBE_URL",
            "https://www.google.com/generate_204",
        ).strip(),
        "probeInterval": os.getenv("XRAY_PROBE_INTERVAL", "20s").strip(),
        "enableConcurrency": parse_bool_env("XRAY_PROBE_CONCURRENCY", True),
    }


def compose_config(src: dict) -> dict:
    outbounds = src.get("outbounds")
    if not isinstance(outbounds, list) or not outbounds:
        raise ValueError("Source config has no outbounds")

    prepared_outbounds = reorder_outbounds(ensure_direct_block(outbounds))
    proxy_tags = extract_proxy_tags(prepared_outbounds)
    config = {
        "log": src.get("log", {"loglevel": "info"}),
        "inbounds": build_inbounds(),
        "outbounds": prepared_outbounds,
        "routing": build_routing(proxy_tags),
    }
    if len(proxy_tags) > 1:
        config["routing"]["balancers"] = [build_balancer(proxy_tags)]
        config["observatory"] = build_observatory(proxy_tags)
    return config


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: compose_xray_config.py <source_json> <output_json>", file=sys.stderr)
        return 2

    src_path = sys.argv[1]
    out_path = sys.argv[2]
    try:
        with open(src_path, "r", encoding="utf-8") as f:
            src = json.load(f)
        out = compose_config(src)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return 0
    except Exception as exc:
        print(f"compose_xray_config.py error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
