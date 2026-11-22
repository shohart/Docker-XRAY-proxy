#!/usr/bin/env python3
"""
sub2xray.py — Universal V2Ray/Xray subscription parser.

Handles ALL common subscription formats without guessing User-Agent:
  1. JSON array of full xray configs  (GhostLine, Capybara with Happ UA)
  2. Single full xray config          (some panels)
  3. Base64-encoded link list          (most providers: Capybara, v2rayNG, etc.)
  4. Plain-text link list              (vless:// vmess:// trojan:// ss:// per line)

Outputs a single xray source config compatible with compose_xray_config.py.

Usage:
  python3 sub2xray.py <subscription_file_or_url> <output_file>

If the first arg starts with http:// or https:// it is fetched via urllib.
"""

import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_SCHEMES = ("vless://", "vmess://", "trojan://", "ss://", "ssr://")
TRAILING_JUNK = ")]},.;'\""

# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "xray"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def load_raw(source: str) -> bytes:
    if source.startswith("http://") or source.startswith("https://"):
        return fetch(source)
    with open(source, "rb") as f:
        return f.read()

# ---------------------------------------------------------------------------
# Base64 helpers
# ---------------------------------------------------------------------------

def b64pad(s: str) -> str:
    return s + "=" * (-len(s) % 4)


def try_b64decode(s: str) -> str | None:
    """Try base64 decode; return decoded string or None."""
    try:
        return base64.urlsafe_b64decode(b64pad(s)).decode("utf-8", errors="replace")
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Link extraction (from html2xray.py — reused, not reinvented)
# ---------------------------------------------------------------------------

def extract_links(text: str) -> list[str]:
    """Extract vless/vmess/trojan/ss/ssr URIs from text."""
    text = text.replace("&amp;", "&")
    pattern = r"(?:vless|vmess|trojan|ssr|ss)://[^\s\"'<>]+"
    links = re.findall(pattern, text, flags=re.IGNORECASE)
    seen = set()
    out = []
    for l in links:
        l = l.strip().rstrip(TRAILING_JUNK)
        key = l.lower()
        if key not in seen and key.startswith(SUPPORTED_SCHEMES):
            seen.add(key)
            out.append(l)
    return out

# ---------------------------------------------------------------------------
# VLESS outbound builder (from html2xray.py — reused)
# ---------------------------------------------------------------------------

def parse_vless(uri: str) -> dict:
    u = urllib.parse.urlsplit(uri)
    userinfo = urllib.parse.unquote(u.username or "")
    host = u.hostname or ""
    port = u.port or 443
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    name = urllib.parse.unquote(u.fragment) if u.fragment else ""
    return {"id": userinfo, "host": host, "port": int(port), "q": q, "name": name}


def build_stream_settings(q: dict) -> dict:
    network = (q.get("type") or q.get("net") or "tcp").lower()
    security = (q.get("security") or "").lower()
    sec = security if security in ("tls", "reality") else ("tls" if security else "none")

    ss = {"network": network, "security": sec}

    if network == "ws":
        ss["wsSettings"] = {
            "path": q.get("path", "/"),
            "headers": {"Host": q["host"]} if q.get("host") else {},
        }
    if network == "grpc":
        ss["grpcSettings"] = {
            "serviceName": q.get("serviceName") or q.get("path") or "",
            "multiMode": False,
        }

    sni = q.get("sni") or q.get("servername") or ""
    alpn = q.get("alpn") or ""
    fp = q.get("fp") or ""

    if sec == "tls":
        tls = {}
        if sni: tls["serverName"] = sni
        if alpn: tls["alpn"] = alpn.split(",")
        if fp: tls["fingerprint"] = fp
        ss["tlsSettings"] = tls

    if sec == "reality":
        reality = {}
        pbk = q.get("pbk") or q.get("publicKey") or ""
        sid = q.get("sid") or q.get("shortId") or ""
        spx = q.get("spx") or q.get("spiderX") or ""
        if sni: reality["serverName"] = sni
        if fp: reality["fingerprint"] = fp
        if pbk: reality["publicKey"] = pbk
        if sid: reality["shortId"] = sid
        if spx: reality["spiderX"] = spx
        ss["realitySettings"] = reality

    return ss


def vless_to_outbound(node: dict, tag: str) -> dict:
    q = node["q"]
    user = {"id": node["id"], "encryption": q.get("encryption", "none")}
    flow = q.get("flow")
    if flow:
        user["flow"] = flow
    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {"vnext": [{"address": node["host"], "port": node["port"], "users": [user]}]},
        "streamSettings": build_stream_settings(q),
    }


def outbound_from_link(link: str, tag: str) -> dict | None:
    """Convert a single vless:// link to an xray outbound dict. Others skipped."""
    ll = link.lower()
    if ll.startswith("vless://"):
        return vless_to_outbound(parse_vless(link), tag)
    # vmess/trojan/ss/ssr — delegate to html2xray.py logic (imported here to avoid duplication)
    # For now, only vless is fully supported; others can be added as needed.
    return None

# ---------------------------------------------------------------------------
# JSON array extractor (from ghostline2xray.py — reused)
# ---------------------------------------------------------------------------

def extract_vless_from_json_configs(configs: list[dict]) -> list[dict]:
    """Extract VLESS outbounds from a list of full xray config objects."""
    seen = set()
    outbounds = []
    for cfg in configs:
        for ob in cfg.get("outbounds", []):
            if ob.get("protocol") != "vless":
                continue
            vnext_list = ob.get("settings", {}).get("vnext", [])
            if not vnext_list:
                continue
            vnext = vnext_list[0]
            addr = vnext.get("address", "")
            port = vnext.get("port", 443)
            users = vnext.get("users", [])
            if not users:
                continue
            uid = users[0].get("id", "")
            key = (addr, port, uid)
            if key in seen:
                continue
            seen.add(key)

            tag = f"node_{len(outbounds)+1}"
            clean = {
                "tag": tag,
                "protocol": "vless",
                "settings": {"vnext": [{"address": addr, "port": port, "users": [
                    {"id": uid, "encryption": users[0].get("encryption", "none")}
                ]}]},
                "streamSettings": ob.get("streamSettings", {}),
            }
            flow = users[0].get("flow", "")
            if flow:
                clean["settings"]["vnext"][0]["users"][0]["flow"] = flow
            outbounds.append(clean)
    return outbounds

# ---------------------------------------------------------------------------
# Build final source config
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dummy-node filter: some providers (Legenda and other INCY/Happ-gated ones)
# serve placeholder vless nodes (address 0.0.0.0:1, names like "Новости") to
# clients that do not identify as the app. Such nodes must never enter
# rotation — they are provider ads, not proxies.
# ---------------------------------------------------------------------------

DUMMY_ADDRESSES = {"0.0.0.0", ""}


def is_dummy_outbound(ob: dict) -> bool:
    vnext = ob.get("settings", {}).get("vnext", [])
    if not vnext:
        return False
    return vnext[0].get("address", "") in DUMMY_ADDRESSES


def build_source_config(outbounds: list[dict]) -> dict:
    http_port = int(os.getenv("HTTP_PROXY_PORT", "3128"))
    socks_port = int(os.getenv("SOCKS_PROXY_PORT", "1080"))
    all_obs = outbounds + [
        {"tag": "direct", "protocol": "freedom", "settings": {}},
        {"tag": "block", "protocol": "blackhole", "settings": {}},
    ]
    return {
        "log": {"loglevel": "info"},
        "inbounds": [
            {"port": http_port, "protocol": "http", "settings": {"users": []},
             "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}},
            {"port": socks_port, "protocol": "socks", "settings": {"auth": "noauth", "udp": True},
             "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}},
        ],
        "outbounds": all_obs,
        "routing": {
            "domainStrategy": "IPOnDemand",
            "rules": [{"type": "field", "ip": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
                        "outboundTag": "direct"}],
        },
    }

# ---------------------------------------------------------------------------
# Main: auto-detect format and parse
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) != 3:
        print("Usage: sub2xray.py <subscription_file_or_url> <output_file>", file=sys.stderr)
        sys.exit(2)

    source, out_file = sys.argv[1], sys.argv[2]
    raw = load_raw(source)

    outbounds = []
    fmt = "unknown"

    # --- Strategy 1: JSON (array or single object) ---
    try:
        data = json.loads(raw)
        configs = []
        if isinstance(data, list):
            configs = data
        elif isinstance(data, dict):
            if "outbounds" in data:
                configs = [data]
            else:
                # wrapped array: {"configs": [...], "data": [...], ...}
                for key in ("configs", "config", "data", "dataObj"):
                    if key in data and isinstance(data[key], list):
                        configs = data[key]
                        break

        if configs:
            outbounds = extract_vless_from_json_configs(configs)
            fmt = f"json_configs({len(configs)} configs, {len(outbounds)} vless)"
    except (json.JSONDecodeError, ValueError):
        pass

    # --- Strategy 2: Base64-encoded link list ---
    if not outbounds:
        text = raw.decode("utf-8", errors="replace").strip()
        decoded = try_b64decode(text)
        if decoded:
            links = extract_links(decoded)
            if links:
                for i, link in enumerate(links, 1):
                    ob = outbound_from_link(link, f"node{i}")
                    if ob:
                        outbounds.append(ob)
                fmt = f"base64_links({len(links)} links, {len(outbounds)} vless)"

    # --- Strategy 3: Plain-text link list ---
    if not outbounds:
        text = raw.decode("utf-8", errors="replace")
        links = extract_links(text)
        if links:
            for i, link in enumerate(links, 1):
                ob = outbound_from_link(link, f"node{i}")
                if ob:
                    outbounds.append(ob)
            fmt = f"plain_links({len(links)} links, {len(outbounds)} vless)"

    if not outbounds:
        print(f"ERROR: No VLESS outbounds found (tried JSON, base64, plain text)", file=sys.stderr)
        sys.exit(1)

    # Drop provider placeholder nodes (0.0.0.0 etc.) — they are not proxies.
    before = len(outbounds)
    outbounds = [ob for ob in outbounds if not is_dummy_outbound(ob)]
    dropped = before - len(outbounds)
    if dropped:
        print(f"[WARN] dropped {dropped} placeholder node(s) (0.0.0.0)")
    if not outbounds:
        print("ERROR: all nodes were provider placeholders (0.0.0.0) — "
              "provider is gating real nodes on the client User-Agent "
              "(Happ/INCY); fetch with happ+ URL prefix", file=sys.stderr)
        sys.exit(1)

    cfg = build_source_config(outbounds)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    print(f"[OK] {fmt} -> {len(outbounds)} vless outbounds -> {out_file}")


if __name__ == "__main__":
    main()
