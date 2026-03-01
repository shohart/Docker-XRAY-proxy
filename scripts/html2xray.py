#!/usr/bin/env python3
import base64
import html
import json
import os
import re
import sys
import urllib.parse

SUPPORTED_SCHEMES = ("vless://", "vmess://", "trojan://", "ss://", "ssr://")
TRAILING_JUNK = ")]},.;'\""


def b64pad(s: str) -> str:
    return s + "=" * (-len(s) % 4)


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(b64pad(s))


def decode_vmess(uri: str) -> dict:
    payload = uri[len("vmess://") :].strip()
    raw = b64d(payload).decode("utf-8", errors="replace")
    return json.loads(raw)


def parse_vless(uri: str) -> dict:
    u = urllib.parse.urlsplit(uri)
    userinfo = urllib.parse.unquote(u.username or "")
    host = u.hostname or ""
    port = u.port or 443
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    name = urllib.parse.unquote(u.fragment) if u.fragment else ""
    return {"id": userinfo, "host": host, "port": int(port), "q": q, "name": name}


def parse_trojan(uri: str) -> dict:
    u = urllib.parse.urlsplit(uri)
    pwd = urllib.parse.unquote(u.username or "")
    host = u.hostname or ""
    port = u.port or 443
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    name = urllib.parse.unquote(u.fragment) if u.fragment else ""
    return {"password": pwd, "host": host, "port": int(port), "q": q, "name": name}


def parse_ss(uri: str) -> dict:
    # Supports:
    # 1) ss://BASE64(method:pass@host:port)#name
    # 2) ss://BASE64(method:pass)@host:port#name (rare)
    # 3) ss://method:pass@host:port#name (sometimes)
    u = urllib.parse.urlsplit(uri)
    name = urllib.parse.unquote(u.fragment) if u.fragment else ""

    if u.netloc:
        netloc = u.netloc
    else:
        # some formats put everything in path
        netloc = u.path.lstrip("/")

    # Strip possible query (plugin) from netloc parsing; ignore plugin
    # urllib already gives query in u.query for standard url forms

    if "@" in netloc and ":" in netloc.split("@", 1)[0]:
        # ss://method:pass@host:port
        cred, hostport = netloc.split("@", 1)
        method, password = cred.split(":", 1)
        host, port = hostport.rsplit(":", 1)
        return {
            "method": method,
            "password": password,
            "host": host,
            "port": int(port),
            "name": name,
        }

    # Try base64 in "userinfo" part: ss://<b64>@host:port OR ss://<b64>#name
    if "@" in netloc:
        b64part, hostport = netloc.split("@", 1)
        cred = b64d(b64part).decode("utf-8", errors="replace")
        method, rest = cred.split(":", 1)
        password, _at = rest.split("@", 1) if "@" in rest else (rest, "")
        host, port = hostport.rsplit(":", 1)
        return {
            "method": method,
            "password": password,
            "host": host,
            "port": int(port),
            "name": name,
        }

    # ss://BASE64(method:pass@host:port)
    try:
        decoded = b64d(netloc).decode("utf-8", errors="replace")
        # decoded should be method:pass@host:port
        method, rest = decoded.split(":", 1)
        password, hostport = rest.split("@", 1)
        host, port = hostport.rsplit(":", 1)
        return {
            "method": method,
            "password": password,
            "host": host,
            "port": int(port),
            "name": name,
        }
    except Exception as e:
        raise ValueError(f"unrecognized ss format: {e}")


def parse_ssr(uri: str) -> dict:
    # ssr://BASE64(host:port:proto:method:obfs:BASE64(password)/?params)
    payload = uri[len("ssr://") :].strip()
    decoded = b64d(payload).decode("utf-8", errors="replace")

    main, _, query = decoded.partition("/?")
    parts = main.split(":")
    if len(parts) < 6:
        raise ValueError("ssr main part invalid")
    host, port, proto, method, obfs, pwd_b64 = parts[:6]
    password = b64d(pwd_b64).decode("utf-8", errors="replace")

    params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
    name = ""
    if "remarks" in params:
        try:
            name = b64d(params["remarks"]).decode("utf-8", errors="replace")
        except Exception:
            name = params["remarks"]

    return {
        "host": host,
        "port": int(port),
        "proto": proto,
        "method": method,
        "obfs": obfs,
        "password": password,
        "params": params,
        "name": name,
    }


def stream_settings_from_common(q: dict, vmess: dict | None = None) -> dict:
    network = (
        q.get("type") or q.get("net") or (vmess.get("net") if vmess else None) or "tcp"
    ).lower()
    security = (q.get("security") or (vmess.get("tls") if vmess else "") or "").lower()

    if security in ("", "none"):
        sec = "none"
    elif security in ("tls",):
        sec = "tls"
    elif security in ("reality",):
        sec = "reality"
    else:
        sec = "tls" if security else "none"

    ss = {"network": network, "security": sec}

    if network == "ws":
        path = q.get("path") or (vmess.get("path") if vmess else None) or "/"
        host = q.get("host") or (vmess.get("host") if vmess else None) or ""
        headers = {}
        if host:
            headers["Host"] = host
        ss["wsSettings"] = {"path": path, "headers": headers}

    if network == "grpc":
        service_name = (
            q.get("serviceName") or (vmess.get("path") if vmess else "") or ""
        )
        ss["grpcSettings"] = {"serviceName": service_name, "multiMode": False}

    sni = (
        q.get("sni")
        or q.get("servername")
        or (vmess.get("sni") if vmess else None)
        or ""
    )
    alpn = q.get("alpn") or ""
    fp = q.get("fp") or ""

    if sec == "tls":
        tls = {}
        if sni:
            tls["serverName"] = sni
        if alpn:
            tls["alpn"] = alpn.split(",")
        if fp:
            tls["fingerprint"] = fp
        ss["tlsSettings"] = tls

    if sec == "reality":
        reality = {}
        pbk = q.get("pbk") or q.get("publicKey") or ""
        sid = q.get("sid") or q.get("shortId") or ""
        spx = q.get("spx") or q.get("spiderX") or ""
        if sni:
            reality["serverName"] = sni
        if fp:
            reality["fingerprint"] = fp
        if pbk:
            reality["publicKey"] = pbk
        if sid:
            reality["shortId"] = sid
        if spx:
            reality["spiderX"] = spx
        ss["realitySettings"] = reality

    return ss


def outbound_from_vless(node: dict, tag: str) -> dict:
    q = node["q"]
    stream = stream_settings_from_common(q)
    user = {"id": node["id"], "encryption": q.get("encryption", "none")}
    flow = q.get("flow")
    if flow:
        user["flow"] = flow
    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [{"address": node["host"], "port": node["port"], "users": [user]}]
        },
        "streamSettings": stream,
    }


def outbound_from_trojan(node: dict, tag: str) -> dict:
    q = node["q"]
    stream = stream_settings_from_common(q)
    return {
        "tag": tag,
        "protocol": "trojan",
        "settings": {
            "servers": [
                {
                    "address": node["host"],
                    "port": node["port"],
                    "password": node["password"],
                }
            ]
        },
        "streamSettings": stream,
    }


def outbound_from_vmess(v: dict, tag: str) -> dict:
    address = v.get("add") or v.get("host") or ""
    port = int(v.get("port") or 443)
    uid = v.get("id") or ""
    aid = int(v.get("aid") or 0)

    q = {
        "type": v.get("net") or v.get("type") or "tcp",
        "host": v.get("host") or "",
        "path": v.get("path") or "/",
        "sni": v.get("sni") or v.get("servername") or "",
    }
    if (v.get("tls") or "").lower() in ("tls", "reality"):
        q["security"] = (v.get("tls") or "").lower()

    stream = stream_settings_from_common(q, vmess=v)

    return {
        "tag": tag,
        "protocol": "vmess",
        "settings": {
            "vnext": [
                {
                    "address": address,
                    "port": port,
                    "users": [{"id": uid, "alterId": aid, "security": "auto"}],
                }
            ]
        },
        "streamSettings": stream,
    }


def outbound_from_ss(node: dict, tag: str) -> dict:
    # Xray outbound shadowsocks
    return {
        "tag": tag,
        "protocol": "shadowsocks",
        "settings": {
            "servers": [
                {
                    "address": node["host"],
                    "port": node["port"],
                    "method": node["method"],
                    "password": node["password"],
                }
            ]
        },
    }


def outbound_from_ssr(node: dict, tag: str) -> dict:
    # Only safely convertible if proto=origin and obfs=plain (i.e., essentially SS)
    proto = (node["proto"] or "").lower()
    obfs = (node["obfs"] or "").lower()
    if proto not in ("origin",) or obfs not in ("plain",):
        raise ValueError(
            f"SSR requires plugins (proto={proto}, obfs={obfs}); cannot convert to native Xray"
        )
    return {
        "tag": tag,
        "protocol": "shadowsocks",
        "settings": {
            "servers": [
                {
                    "address": node["host"],
                    "port": node["port"],
                    "method": node["method"],
                    "password": node["password"],
                }
            ]
        },
    }


def extract_links(text: str) -> list[str]:
    text = html.unescape(text)
    pattern = r'(?:vless|vmess|trojan|ssr|ss)://[^\s"\'<>]+'
    links = re.findall(pattern, text, flags=re.IGNORECASE)
    seen = set()
    out = []
    for l in links:
        l = l.strip().rstrip(TRAILING_JUNK)
        ll = l.lower()
        if ll.startswith(SUPPORTED_SCHEMES):
            key = ll
            if key not in seen:
                seen.add(key)
                out.append(l)
    return out


def build_config(links: list[str]) -> dict:
    http_port = int(os.getenv("HTTP_PROXY_PORT", "3128"))
    socks_port = int(os.getenv("SOCKS_PROXY_PORT", "1080"))

    inbounds = [
        {
            "port": http_port,
            "protocol": "http",
            "settings": {"users": []},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        },
        {
            "port": socks_port,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": True},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        },
    ]

    outbounds = []
    ok = 0
    for i, link in enumerate(links, start=1):
        tag = f"node{i}"
        try:
            ll = link.lower()
            if ll.startswith("vless://"):
                outbounds.append(outbound_from_vless(parse_vless(link), tag))
                ok += 1
            elif ll.startswith("trojan://"):
                outbounds.append(outbound_from_trojan(parse_trojan(link), tag))
                ok += 1
            elif ll.startswith("vmess://"):
                outbounds.append(outbound_from_vmess(decode_vmess(link), tag))
                ok += 1
            elif ll.startswith("ss://"):
                outbounds.append(outbound_from_ss(parse_ss(link), tag))
                ok += 1
            elif ll.startswith("ssr://"):
                outbounds.append(outbound_from_ssr(parse_ssr(link), tag))
                ok += 1
        except Exception as e:
            print(f"[WARN] skip {tag} ({link[:32]}...): {e}", file=sys.stderr)

    if ok == 0:
        raise SystemExit("No valid nodes parsed (all failed/unsupported)")

    outbounds.append({"tag": "direct", "protocol": "freedom", "settings": {}})
    outbounds.append({"tag": "block", "protocol": "blackhole", "settings": {}})

    return {
        "log": {"loglevel": "info"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {
            "domainStrategy": "IPOnDemand",
            "rules": [
                {
                    "type": "field",
                    "ip": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
                    "outboundTag": "direct",
                }
            ],
        },
    }


def main():
    if len(sys.argv) != 3:
        print(
            "Usage: html2xray.py <html_or_text_file> <output_config>", file=sys.stderr
        )
        sys.exit(2)
    in_file, out_file = sys.argv[1], sys.argv[2]
    text = open(in_file, "r", encoding="utf-8", errors="replace").read()
    links = extract_links(text)

    # Fallback: some providers return subscription as one base64 blob (list of links)
    if not links:
        candidate = text.strip()
        # remove whitespace
        candidate = re.sub(r"\s+", "", candidate)
        # try base64 decode whole payload
        try:
            decoded = b64d(candidate).decode("utf-8", errors="replace")
            links = extract_links(decoded)
            if links:
                text = decoded
        except Exception:
            pass

    if not links:
        raise SystemExit("No vless/vmess/trojan/ss/ssr links found (direct or base64)")

    cfg = build_config(links)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(
        f"[OK] links_found={len(links)} outbounds_ok={len(cfg['outbounds'])-2} wrote={out_file}"
    )


if __name__ == "__main__":
    main()
