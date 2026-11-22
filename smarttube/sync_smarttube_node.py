#!/usr/bin/env python3
"""
sync_smarttube_node.py

Keeps the standalone SmartTube proxy (container xray-smarttube, in smarttube/)
pinned to the best YouTube-capable VLESS node from the main subscription config.

Runs on a cron. On each run:

  1. Read the MAIN subscription config (config/config.json) — rewritten hourly
     by the subscription updater.
  2. Enumerate ALL VLESS outbounds (protocol == vless; excludes openvpn-out).
  3. Probe every node IN PARALLEL: spin up an isolated xray instance on a
     random local port and test YouTube suitability:
        * https://www.youtube.com/                     == 200
        * https://redirector.googlevideo.com/generate_204 == 204
        * exit IP defined AND not Cloudflare (Cloudflare egress breaks
          SmartTube's InnerTube path)
     Record probe time; the fastest suitable node wins.
  4. Compare the winner to the current smarttube/config.json outbound.
     - same  -> nothing to do
     - diff  -> rewrite config, restart container, re-verify via the container
                ports (1082/3129); on failure roll back.
  5. Send a Telegram notification when the node changes or when anything fails.

Idempotent, safe to run every few minutes. Logs to smarttube/sync.log.
"""

import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

REPO = "/home/shohart/repositories/Docker-XRAY-proxy"
MAIN_CFG = os.path.join(REPO, "config", "config.json")
ST_DIR = os.path.join(REPO, "smarttube")
ST_CFG = os.path.join(ST_DIR, "config.json")
ST_BAK = os.path.join(ST_DIR, "config.json.bak")
LOG = os.path.join(ST_DIR, "sync.log")
XRAY_BIN = os.path.join(ST_DIR, "xray")   # static xray binary (auto-restored if missing)
HERMES_ENV = "/home/shohart/.hermes/.env"

CONTAINER = "xray-smarttube"
CONTAINER_SOCKS_PORT = 1082

TG_BOT_TOKEN = None
TG_CHAT_ID = None

YT_MAIN = "https://www.youtube.com/"
GOOGLEVIDEO = "https://redirector.googlevideo.com/generate_204"
IFCONFIG = "https://ifconfig.me"

PROBE_TIMEOUT = 15           # per-target curl timeout, seconds
CONNECT_TIMEOUT = 12         # curl connect timeout, seconds
NODE_SETTLE = 2.0            # seconds to let an isolated xray instance bind
MAX_WORKERS = 8              # parallel node probes


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def load_telegram_creds() -> None:
    global TG_BOT_TOKEN, TG_CHAT_ID
    if TG_BOT_TOKEN:
        return
    try:
        with open(HERMES_ENV, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    TG_BOT_TOKEN = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("TELEGRAM_HOME_CHANNEL="):
                    TG_CHAT_ID = line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError as e:
        log(f"WARN cannot read telegram creds: {e}")
    if not TG_CHAT_ID:
        TG_CHAT_ID = os.getenv("SMARTTUBE_TG_CHAT_ID") or None


def tg_send(text: str) -> bool:
    load_telegram_creds()
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("WARN telegram not configured, skip notify")
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TG_CHAT_ID, "text": text,
                       "disable_web_page_preview": True}).encode()
    try:
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode())
            if body.get("ok"):
                return True
            log(f"WARN telegram send failed: {body}")
            return False
    except Exception as e:
        log(f"WARN telegram send error: {e}")
        return False


# --------------------------------------------------------------------------
# JSON helpers
# --------------------------------------------------------------------------

def load_json(path: str):
    with open(path, "r") as f:
        return json.load(f)


def list_vless_outbounds(cfg):
    """Return [(tag, outbound), ...] for all VLESS nodes (no openvpn)."""
    out = []
    for o in cfg.get("outbounds", []):
        if o.get("protocol") != "vless":
            continue
        tag = o.get("tag", "")
        if tag in ("openvpn-out", "block"):
            continue
        out.append((tag, o))
    return out


def outbound_identity(o):
    """Stable fingerprint of a node's connection parameters."""
    try:
        v = o["settings"]["vnext"][0]
        return {
            "address": v.get("address"),
            "port": v.get("port"),
            "id": v.get("users", [{}])[0].get("id"),
            "stream": o.get("streamSettings"),
        }
    except Exception:
        return o


def node_address(o):
    try:
        v = o["settings"]["vnext"][0]
        return f"{v.get('address')}:{v.get('port')}"
    except Exception:
        return "?"


# --------------------------------------------------------------------------
# Container state
# --------------------------------------------------------------------------

def container_running() -> bool:
    r = subprocess.run(["docker", "ps", "-q", "-f", f"name={CONTAINER}"],
                       capture_output=True, text=True, timeout=15)
    return bool(r.stdout.strip())


def ensure_container_running() -> None:
    if container_running():
        return
    log(f"WARN container {CONTAINER} not running, starting")
    subprocess.run(["docker", "start", CONTAINER],
                   capture_output=True, text=True, timeout=60)


def restart_container() -> None:
    subprocess.run(["docker", "restart", CONTAINER],
                   capture_output=True, text=True, timeout=60)


def current_smarttube_identity():
    try:
        cfg = load_json(ST_CFG)
    except Exception:
        return None
    for o in cfg.get("outbounds", []):
        if o.get("tag") == "node_9":
            return outbound_identity(o)
    return None


# --------------------------------------------------------------------------
# Isolated node probing (parallel-safe)
# --------------------------------------------------------------------------

def free_port() -> int:
    for _ in range(50):
        p = random.randint(12000, 48000)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free port")


def build_isolated_config(node_outbound, port):
    """Minimal xray config: one socks inbound -> the node outbound."""
    node = json.loads(json.dumps(node_outbound))
    node["tag"] = "t"
    return {
        "log": {"loglevel": "error"},
        "inbounds": [{
            "tag": "socks",
            "listen": "127.0.0.1",
            "port": port,
            "protocol": "socks",
            "settings": {"udp": True},
        }],
        "outbounds": [node, {"tag": "block", "protocol": "blackhole"}],
        "routing": {"rules": [{
            "type": "field", "inboundTag": ["socks"], "outboundTag": "t",
        }]},
    }


def curl_socks_code(proxy_port, url, timeout=PROBE_TIMEOUT):
    try:
        r = subprocess.run(
            ["curl", "--socks5", f"127.0.0.1:{proxy_port}",
             "--connect-timeout", str(CONNECT_TIMEOUT),
             "-m", str(timeout), "-s", "-o", "/dev/null",
             "-w", "%{http_code};%{time_total}", "-L", url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        code, _, t = r.stdout.strip().partition(";")
        return code, float(t or 0)
    except Exception:
        return "ERR", 0.0


def curl_socks_ip(proxy_port, timeout=PROBE_TIMEOUT):
    try:
        r = subprocess.run(
            ["curl", "--socks5", f"127.0.0.1:{proxy_port}",
             "--connect-timeout", str(CONNECT_TIMEOUT),
             "-m", str(timeout), "-s", IFCONFIG],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def is_cloudflare(ip):
    if not ip:
        return None
    try:
        with urllib.request.urlopen(
                f"https://ipinfo.io/{ip}/json", timeout=10) as r:
            d = json.loads(r.read().decode())
        return "cloudflare" in (d.get("org") or "").lower()
    except Exception:
        return None


def ensure_xray_bin() -> None:
    """Restore the static xray binary from the stack container if missing."""
    if os.path.exists(XRAY_BIN) and os.access(XRAY_BIN, os.X_OK):
        return
    log("WARN xray binary missing, restoring from stack container")
    r = subprocess.run(
        ["docker", "cp", "xray:/usr/local/bin/xray", XRAY_BIN],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode == 0:
        os.chmod(XRAY_BIN, 0o755)
        log("xray binary restored")
    else:
        raise RuntimeError(f"cannot restore xray binary: {r.stderr.strip()}")


def probe_node(tag, node_outbound):
    """Probe one node in isolation. Returns a result tuple."""
    port = free_port()
    cfg_path = f"/tmp/xray_node_probe_{port}.json"
    with open(cfg_path, "w") as f:
        json.dump(build_isolated_config(node_outbound, port), f)

    proc = subprocess.Popen(
        [XRAY_BIN, "run", "-c", cfg_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(NODE_SETTLE)
        yt_code, yt_time = curl_socks_code(port, YT_MAIN)
        gv_code, gv_time = curl_socks_code(port, GOOGLEVIDEO)
        ip = curl_socks_ip(port)
        cf = is_cloudflare(ip)
        suitable = (yt_code == "200" and gv_code in ("200", "204")
                    and bool(ip) and cf is False)
        total = round(yt_time + gv_time, 2)
        addr = node_address(node_outbound)
        return {
            "tag": tag, "addr": addr, "suitable": suitable,
            "total": total, "yt": yt_code, "ytt": yt_time,
            "gv": gv_code, "gvt": gv_time, "ip": ip, "cf": cf,
        }
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            os.remove(cfg_path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    load_telegram_creds()
    ensure_xray_bin()
    main_cfg = load_json(MAIN_CFG)
    nodes = list_vless_outbounds(main_cfg)

    if not nodes:
        log("WARN no VLESS nodes in main config")
        ensure_container_running()
        return 0

    log(f"scanning {len(nodes)} VLESS nodes (parallel x{MAX_WORKERS})...")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(probe_node, tag, node): tag for tag, node in nodes}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:
                log(f"ERROR probing {futs[fut]}: {e}")

    # sort for stable logging
    results.sort(key=lambda r: r["tag"])
    for r in results:
        log(f"  {r['tag']:<10} {r['addr']:<24} suitable={r['suitable']!s:<5} "
            f"yt={r['yt']}({r['ytt']:.2f}s) gv={r['gv']}({r['gvt']:.2f}s) "
            f"ip={r['ip'] or '-'} cf={r['cf']}")

    suitable = [r for r in results if r["suitable"]]
    if not suitable:
        msg = ("⚠️ SmartTube: ни одна VLESS-нода не проходит проверку YouTube "
               "(googlevideo + не-Cloudflare). Конфиг smarttube не меняю.")
        log("FAIL no suitable node for YouTube")
        tg_send(msg)
        ensure_container_running()
        return 1

    best = min(suitable, key=lambda r: r["total"])
    log(f"best node: {best['tag']} ({best['addr']}) total={best['total']}s "
        f"ip={best['ip']}")

    best_node = next(n for t, n in nodes if t == best["tag"])
    best_id = outbound_identity(best_node)
    cur_id = current_smarttube_identity()

    if cur_id == best_id:
        log(f"no change: smarttube already on {best['tag']}")
        ensure_container_running()
        return 0

    # --- change: update + verify ---
    old_addr = cur_id.get("address") if cur_id else "?"
    log(f"CHANGE: switching smarttube to {best['tag']} ({best['addr']})")

    shutil.copy2(ST_CFG, ST_BAK)

    st_cfg = load_json(ST_CFG)
    replaced = False
    for i, o in enumerate(st_cfg.get("outbounds", [])):
        if o.get("tag") == "node_9":
            st_cfg["outbounds"][i] = json.loads(json.dumps(best_node))
            st_cfg["outbounds"][i]["tag"] = "node_9"
            replaced = True
            break
    if not replaced:
        log("ERROR node_9 outbound missing from smarttube config")
        return 1

    with open(ST_CFG, "w") as f:
        json.dump(st_cfg, f, indent=2, ensure_ascii=False)

    restart_container()
    time.sleep(4)

    gv_c, gv_t = curl_socks_code(CONTAINER_SOCKS_PORT, GOOGLEVIDEO)
    yt_c, yt_t = curl_socks_code(CONTAINER_SOCKS_PORT, YT_MAIN)
    ip = curl_socks_ip(CONTAINER_SOCKS_PORT)
    cf = is_cloudflare(ip)
    ok = (yt_c == "200" and gv_c in ("200", "204")
          and bool(ip) and cf is False)

    if ok:
        log(f"OK smarttube now on {best['tag']} ({best['addr']}) via container")
        tg_send(
            f"🎬 SmartTube: переключил на ноду *{best['tag']}*\n"
            f"Выход: {best['addr']} → {ip}\n"
            f"yt={yt_c} ({yt_t:.2f}s) · gv={gv_c} ({gv_t:.2f}s) · не-Cloudflare"
        )
        return 0

    log(f"FAIL container verify after switch: yt={yt_c} gv={gv_c} ip={ip!r} "
        f"cf={cf} — rolling back")
    shutil.copy2(ST_BAK, ST_CFG)
    restart_container()
    tg_send("⚠️ SmartTube: переключение не прошло проверку после применения, "
            "откатил на прежнюю ноду.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"FATAL {type(e).__name__}: {e}")
        try:
            tg_send(f"⚠️ SmartTube sync: ошибка {type(e).__name__}: {e}")
        except Exception:
            pass
        sys.exit(1)
