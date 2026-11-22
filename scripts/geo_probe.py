"""Exit-country probing for proxy nodes (stdlib only + an xray binary).

The live xray is never touched: a temporary, isolated xray process is started
with one HTTP inbound per node on 127.0.0.1 (inbound geo-<tag> -> outbound
<tag>), and a geo service is queried through each of them. The answer is the
country of the node's real *exit* IP, which may differ from what the provider
put into the node name (bridges, CDN-fronted nodes) and is the only source
for nodes without any country in the name.

Results are cached in data/node_geo.json keyed by the node *endpoint*
(address, port, user id), so they survive tag renumbering when the
subscription set changes. A tag index ("nodes") is rebuilt against the
current config for readers (tunnelctl, the daemon's country filters).
"""

from __future__ import annotations

import concurrent.futures as cf
import copy
import datetime as dt
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

SKIP_PROTOCOLS = ("freedom", "blackhole", "dns")

# (url, parser) tried in order until one yields a country code.
# Cloudflare trace returns "loc=NL" / "ip=..." for the client (= node exit) IP.
GEO_SOURCES = [
    ("https://www.cloudflare.com/cdn-cgi/trace", "cf_trace"),
    ("https://ipinfo.io/json", "ipinfo"),
    ("http://ip-api.com/json/?fields=status,countryCode,query", "ipapi"),
]
USER_AGENT = "curl/8.5.0"


def endpoint_key(ob: dict) -> str:
    st = ob.get("settings", {}) or {}
    servers = st.get("vnext") or st.get("servers") or [{}]
    srv = servers[0] if servers else {}
    users = srv.get("users") or [{}]
    uid = (users[0] or {}).get("id") or (users[0] or {}).get("user") or srv.get("password") or ""
    raw = f"{ob.get('protocol')}|{srv.get('address', '')}|{srv.get('port', '')}|{uid}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def probe_targets(config: dict) -> list[dict]:
    """Proxy outbounds of the live config worth probing (balancer nodes + fallback)."""
    obs = [o for o in config.get("outbounds", []) if o.get("tag") and o.get("protocol") not in SKIP_PROTOCOLS]
    return obs


def free_ports(n: int) -> list[int]:
    socks, ports = [], []
    try:
        for _ in range(n):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            socks.append(s)
            ports.append(s.getsockname()[1])
    finally:
        for s in socks:
            s.close()
    return ports


def build_probe_config(outbounds: list[dict], ports: list[int]) -> tuple[dict, dict[str, int]]:
    inbounds, rules, mapping, clean_obs = [], [], {}, []
    for ob, port in zip(outbounds, ports):
        tag = ob["tag"]
        o = copy.deepcopy(ob)
        o.pop("_meta", None)
        clean_obs.append(o)
        inbounds.append({"tag": f"geo-{tag}", "listen": "127.0.0.1", "port": port, "protocol": "http",
                         "settings": {}})
        rules.append({"type": "field", "inboundTag": [f"geo-{tag}"], "outboundTag": tag})
        mapping[tag] = port
    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": clean_obs + [{"tag": "block", "protocol": "blackhole", "settings": {}}],
        # anything not matched (should not happen) is dropped, never sent direct
        "routing": {"rules": rules + [{"type": "field", "network": "tcp,udp", "outboundTag": "block"}]},
    }
    return cfg, mapping


def parse_geo(kind: str, body: str) -> tuple[str | None, str | None]:
    """-> (ISO country, exit ip)"""
    if kind == "cf_trace":
        kv = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
        cc = kv.get("loc", "").upper()
        return (cc if re.fullmatch(r"[A-Z]{2}", cc) and cc != "XX" else None), kv.get("ip")
    data = json.loads(body)
    if kind == "ipinfo":
        cc = str(data.get("country", "")).upper()
        return (cc if re.fullmatch(r"[A-Z]{2}", cc) else None), data.get("ip")
    if kind == "ipapi":
        cc = str(data.get("countryCode", "")).upper()
        ok = data.get("status") == "success" and re.fullmatch(r"[A-Z]{2}", cc)
        return (cc if ok else None), data.get("query")
    return None, None


def probe_one(port: int, timeout: float = 12.0) -> dict:
    proxy = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    errors = []
    for url, kind in GEO_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with opener.open(req, timeout=timeout) as r:
                body = r.read(65536).decode("utf-8", "replace")
            cc, ip = parse_geo(kind, body)
            if cc:
                return {"ok": True, "country": cc, "ip": ip, "via": kind}
            errors.append(f"{kind}: no country")
        except Exception as e:  # noqa: BLE001 - any failure -> try the next source
            errors.append(f"{kind}: {type(e).__name__}: {str(e)[:80]}")
    return {"ok": False, "error": "; ".join(errors)}


def _wait_listening(ports: list[int], deadline: float, proc=None) -> bool:
    pending = set(ports)
    while pending and time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        for p in list(pending):
            with socket.socket() as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", p)) == 0:
                    pending.discard(p)
        if pending:
            time.sleep(0.2)
    return not pending


class ProbeStartError(RuntimeError):
    pass


def run_probe(outbounds: list[dict], xray_bin: str = "xray", concurrency: int = 6,
              timeout: float = 12.0, log=None) -> dict[str, dict]:
    """Probe the exit country of every outbound. Returns tag -> result.

    All nodes share one temporary xray. If it refuses to start (one outbound
    the binary cannot parse kills the whole batch), every node is retried in
    its own process so a single bad node only fails itself.
    """
    if not outbounds:
        return {}
    try:
        return _run_batch(outbounds, xray_bin, concurrency, timeout)
    except ProbeStartError as e:
        if len(outbounds) == 1:
            return {outbounds[0]["tag"]: {"ok": False, "error": str(e)}}
        if log:
            log(f"batch probe failed ({e}); probing nodes one by one")
    results: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, min(concurrency, 4))) as ex:
        futs = [ex.submit(run_probe, [ob], xray_bin, 1, timeout) for ob in outbounds]
        for fut in cf.as_completed(futs):
            results.update(fut.result())
    return results


def _run_batch(outbounds, xray_bin, concurrency, timeout) -> dict[str, dict]:
    ports = free_ports(len(outbounds))
    cfg, mapping = build_probe_config(outbounds, ports)
    fd, path = tempfile.mkstemp(prefix="geoprobe-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    logf = tempfile.TemporaryFile(mode="w+")
    proc = None
    try:
        try:
            proc = subprocess.Popen([xray_bin, "run", "-c", path], stdout=logf, stderr=subprocess.STDOUT)
        except OSError as e:
            raise ProbeStartError(f"cannot run {xray_bin}: {e}")
        if not _wait_listening(ports, time.time() + 10, proc):
            logf.seek(0)
            lines = [ln for ln in logf.read().splitlines() if "Failed" in ln or "failed" in ln or "rror" in ln]
            raise ProbeStartError("probe xray did not start: " + (lines[-1][-240:] if lines else "ports not listening"))
        results = {}
        with cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
            futs = {ex.submit(probe_one, port, timeout): tag for tag, port in mapping.items()}
            for fut in cf.as_completed(futs):
                results[futs[fut]] = fut.result()
        return results
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        logf.close()
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def load_cache(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("by_key", {})
        data.setdefault("nodes", {})
        return data
    except FileNotFoundError:
        return {"by_key": {}, "nodes": {}}
    except Exception:
        return {"by_key": {}, "nodes": {}}


def save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o644)
    tmp.replace(path)


def reindex(cache: dict, outbounds: list[dict]) -> dict:
    """Rebuild the tag -> result index for the current config."""
    nodes = {}
    for ob in outbounds:
        entry = cache["by_key"].get(endpoint_key(ob))
        if entry and entry.get("country"):
            nodes[ob["tag"]] = {k: entry.get(k) for k in ("country", "ip", "checked_at", "via")}
    cache["nodes"] = nodes
    cache["indexed_at"] = _now().isoformat(timespec="seconds")
    return cache


def merge_results(cache: dict, outbounds: list[dict], results: dict[str, dict]) -> dict:
    by_tag = {ob["tag"]: ob for ob in outbounds}
    now = _now().isoformat(timespec="seconds")
    for tag, res in results.items():
        ob = by_tag.get(tag)
        if not ob:
            continue
        key = endpoint_key(ob)
        prev = cache["by_key"].get(key, {})
        if res.get("ok"):
            cache["by_key"][key] = {"country": res["country"], "ip": res.get("ip"), "via": res.get("via"),
                                    "checked_at": now, "last_tag": tag, "failures": 0}
        else:
            # keep a previously known country; just record the failed attempt
            prev = dict(prev)
            prev.update({"last_error": res.get("error", ""), "failed_at": now, "last_tag": tag,
                         "failures": int(prev.get("failures", 0)) + 1})
            cache["by_key"][key] = prev
    return reindex(cache, outbounds)


def due_for_probe(cache: dict, outbounds: list[dict], refresh_h: float, retry_min: float,
                  now: dt.datetime | None = None) -> list[dict]:
    """Outbounds without a fresh result (new, stale, or failed long enough ago)."""
    now = now or _now()
    out = []
    for ob in outbounds:
        e = cache["by_key"].get(endpoint_key(ob))
        if not e:
            out.append(ob)
            continue
        failed_at = _parse(e.get("failed_at"))
        checked_at = _parse(e.get("checked_at"))
        if failed_at and (not checked_at or failed_at > checked_at):
            if now - failed_at >= dt.timedelta(minutes=retry_min * min(8, 2 ** max(0, e.get("failures", 1) - 1))):
                out.append(ob)
            continue
        if not checked_at or now - checked_at >= dt.timedelta(hours=refresh_h):
            out.append(ob)
    return out


def _parse(raw):
    if not raw:
        return None
    try:
        t = dt.datetime.fromisoformat(raw)
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


if __name__ == "__main__":  # manual run: python3 geo_probe.py /etc/xray/config.json [tag...]
    import sys
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    targets = probe_targets(cfg)
    if len(sys.argv) > 2:
        targets = [o for o in targets if o["tag"] in sys.argv[2:]]
    for tag, res in sorted(run_probe(targets, os.getenv("XRAY_BIN", "xray")).items()):
        print(tag, json.dumps(res, ensure_ascii=False))
