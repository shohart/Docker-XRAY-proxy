#!/usr/bin/env python3
"""Shared, dependency-free logic for tunnelctl, the sticky balancer and compose.

Everything here is pure (no Docker, no gRPC) so it can be unit-tested and
imported from the host CLI, the balancer container and the updater container.

Contents:
  * country detection from provider remarks (flag emoji, names, city codes)
  * ISO code -> flag emoji
  * control-file ("tunnel policy") defaults/loading/saving
  * node filtering by country rules (all / only / except)
  * node selection strategies (best / next / random) and rotation schedules
  * per-client pin rules + balancers for the Xray routing section
  * Xray access-log parsing -> active clients
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import random
import re
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Countries
# ---------------------------------------------------------------------------

# ISO -> (English name, Russian name). Used for display and name matching.
COUNTRIES: dict[str, tuple[str, str]] = {
    "AE": ("UAE", "ОАЭ"), "AM": ("Armenia", "Армения"), "AR": ("Argentina", "Аргентина"),
    "AT": ("Austria", "Австрия"), "AU": ("Australia", "Австралия"), "AZ": ("Azerbaijan", "Азербайджан"),
    "BE": ("Belgium", "Бельгия"), "BG": ("Bulgaria", "Болгария"), "BR": ("Brazil", "Бразилия"),
    "BY": ("Belarus", "Беларусь"), "CA": ("Canada", "Канада"), "CH": ("Switzerland", "Швейцария"),
    "CN": ("China", "Китай"), "CY": ("Cyprus", "Кипр"), "CZ": ("Czechia", "Чехия"),
    "DE": ("Germany", "Германия"), "DK": ("Denmark", "Дания"), "EE": ("Estonia", "Эстония"),
    "ES": ("Spain", "Испания"), "FI": ("Finland", "Финляндия"), "FR": ("France", "Франция"),
    "GB": ("United Kingdom", "Великобритания"), "GE": ("Georgia", "Грузия"), "GR": ("Greece", "Греция"),
    "HK": ("Hong Kong", "Гонконг"), "HR": ("Croatia", "Хорватия"), "HU": ("Hungary", "Венгрия"),
    "ID": ("Indonesia", "Индонезия"), "IE": ("Ireland", "Ирландия"), "IL": ("Israel", "Израиль"),
    "IN": ("India", "Индия"), "IS": ("Iceland", "Исландия"), "IT": ("Italy", "Италия"),
    "JP": ("Japan", "Япония"), "KG": ("Kyrgyzstan", "Киргизия"), "KR": ("South Korea", "Корея"),
    "KZ": ("Kazakhstan", "Казахстан"), "LT": ("Lithuania", "Литва"), "LU": ("Luxembourg", "Люксембург"),
    "LV": ("Latvia", "Латвия"), "MD": ("Moldova", "Молдова"), "MX": ("Mexico", "Мексика"),
    "MY": ("Malaysia", "Малайзия"), "NL": ("Netherlands", "Нидерланды"), "NO": ("Norway", "Норвегия"),
    "PL": ("Poland", "Польша"), "PT": ("Portugal", "Португалия"), "RO": ("Romania", "Румыния"),
    "RS": ("Serbia", "Сербия"), "RU": ("Russia", "Россия"), "SE": ("Sweden", "Швеция"),
    "SG": ("Singapore", "Сингапур"), "SI": ("Slovenia", "Словения"), "SK": ("Slovakia", "Словакия"),
    "TH": ("Thailand", "Таиланд"), "TR": ("Turkey", "Турция"), "TW": ("Taiwan", "Тайвань"),
    "UA": ("Ukraine", "Украина"), "US": ("USA", "США"), "UZ": ("Uzbekistan", "Узбекистан"),
    "VN": ("Vietnam", "Вьетнам"),
}

# Extra spellings / inflections / cities / airport codes, matched as whole words
# (case-insensitive). Order inside the remark decides when several match.
_NAME_ALIASES: dict[str, list[str]] = {
    "AE": ["united arab emirates", "emirates", "dubai", "эмираты", "дубай"],
    "AM": ["армении", "ереван", "yerevan"],
    "AT": ["австрии", "вена", "vienna"],
    "BE": ["бельгии", "brussels"],
    "BG": ["болгарии", "sofia"],
    "BY": ["belarus", "белоруссия", "минск"],
    "CA": ["канаде", "toronto", "торонто", "montreal"],
    "CH": ["швейцарии", "zurich", "цюрих"],
    "CZ": ["czech", "czech republic", "чехии", "prague", "прага"],
    "DE": ["deutschland", "германии", "frankfurt", "франкфурт", "berlin", "берлин", "falkenstein", "nuremberg"],
    "DK": ["дании", "copenhagen"],
    "EE": ["эстонии", "tallinn", "таллин", "таллинн"],
    "ES": ["испании", "madrid", "мадрид"],
    "FI": ["финляндии", "helsinki", "хельсинки"],
    "FR": ["франции", "paris", "париж"],
    "GB": ["uk", "great britain", "britain", "england", "англия", "британия", "london", "лондон"],
    "HK": ["hongkong"],
    "IN": ["индии"],
    "IT": ["италии", "milan", "милан", "rome"],
    "JP": ["японии", "tokyo", "токио"],
    "KR": ["korea", "кореи", "seoul", "сеул"],
    "KZ": ["казахстане", "almaty", "алматы"],
    "LT": ["литве", "vilnius", "вильнюс"],
    "LV": ["латвии", "riga", "рига"],
    "MD": ["молдавия", "chisinau", "кишинёв", "кишинев"],
    "NL": ["holland", "the netherlands", "нидерландах", "нидерландов", "голландия", "amsterdam", "амстердам"],
    "NO": ["норвегии", "oslo"],
    "PL": ["польше", "польши", "warsaw", "варшава"],
    "RO": ["румынии", "bucharest"],
    "RS": ["сербии", "belgrade", "белград"],
    "RU": ["россии", "рф", "moscow", "москва", "msk", "spb", "петербург"],
    "SE": ["швеции", "stockholm", "стокгольм"],
    "SG": ["сингапуре"],
    "TR": ["türkiye", "turkiye", "турции", "istanbul", "стамбул"],
    "UA": ["украины", "kyiv", "kiev", "киев"],
    "US": ["united states", "america", "америка", "new york", "нью-йорк", "los angeles", "chicago", "dallas"],
}

# Short tokens used in provider tags/hostnames (e.g. GhostLine "ams_1").
_CODE_HINTS: dict[str, str] = {
    "ams": "NL", "nl": "NL", "fra": "DE", "fr2": "FR", "de": "DE", "hel": "FI", "fi": "FI",
    "swe": "SE", "sto": "SE", "se": "SE", "est": "EE", "ee": "EE", "can": "CA", "tor": "CA",
    "waw": "PL", "pl": "PL", "lon": "GB", "uk": "GB", "gb": "GB", "par": "FR",
    "riga": "LV", "lv": "LV", "lt": "LT", "ist": "TR", "tr": "TR", "kz": "KZ", "ala": "KZ",
    "nyc": "US", "usa": "US", "us": "US", "zrh": "CH", "ch": "CH", "vie": "AT", "at": "AT",
    "sgp": "SG", "sg": "SG", "tyo": "JP", "jp": "JP", "msk": "RU", "spb": "RU", "mow": "RU",
    "es": "ES", "it": "IT", "cz": "CZ", "ro": "RO", "md": "MD", "rs": "RS", "am": "AM", "ge": "GE",
}

_WORD = r"[0-9a-zа-яё]"


def _build_name_patterns() -> list[tuple[re.Pattern, str]]:
    pats: list[tuple[re.Pattern, str]] = []
    for iso, (en, ru) in COUNTRIES.items():
        words = {en.lower(), ru.lower(), *(a.lower() for a in _NAME_ALIASES.get(iso, []))}
        for w in words:
            if len(w) < 3 and w not in ("рф",):
                continue
            pats.append((re.compile(rf"(?<!{_WORD}){re.escape(w)}(?!{_WORD})", re.IGNORECASE), iso))
    return pats


_NAME_PATTERNS = _build_name_patterns()
_FLAG_RE = re.compile("([\U0001F1E6-\U0001F1FF])([\U0001F1E6-\U0001F1FF])")


def flag(iso: str | None) -> str:
    """ISO 3166-1 alpha-2 -> flag emoji ('??' or empty -> white flag)."""
    if not iso or len(iso) != 2 or not iso.isalpha() or iso.upper() == "??":
        return "\U0001F310"  # 🌐 (reliably 2 cells wide, unlike 🏳)
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso.upper())


def country_name(iso: str | None, lang: str = "en") -> str:
    if not iso or iso not in COUNTRIES:
        return "Unknown" if lang == "en" else "Неизвестно"
    return COUNTRIES[iso][0 if lang == "en" else 1]


def country_from_text(text: str) -> str | None:
    """Earliest country *name* mentioned in text (beats the flag: '🇷🇺 МОСТ Netherlands' -> NL)."""
    if not text:
        return None
    best: tuple[int, str] | None = None
    for pat, iso in _NAME_PATTERNS:
        m = pat.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), iso)
    return best[1] if best else None


def country_from_flag(text: str) -> str | None:
    if not text:
        return None
    m = _FLAG_RE.search(text)
    if not m:
        return None
    return "".join(chr(ord(ch) - 0x1F1E6 + ord("A")) for ch in m.groups())


def country_from_hint(*hints: str) -> str | None:
    """Look for short codes in provider tags/hostnames ('ams_1', 'bridge-nl2.x.blog')."""
    for hint in hints:
        if not hint:
            continue
        labels = hint.lower().split(".")
        if len(labels) > 1:
            labels = labels[:-1]  # drop TLD (".ru" says nothing about the exit)
        for label in labels:
            for tok in re.split(r"[-_\d]+", label):
                if tok in _CODE_HINTS:
                    return _CODE_HINTS[tok]
    return None


def detect_country(name: str = "", *hints: str) -> tuple[str, str]:
    """Return (iso, source) where source is name|flag|hint|unknown."""
    iso = country_from_text(name)
    if iso:
        return iso, "name"
    iso = country_from_flag(name)
    if iso:
        return iso, "flag"
    iso = country_from_hint(*hints)
    if iso:
        return iso, "hint"
    return "??", "unknown"


def remark_specificity(name: str) -> int:
    """How useful a remark is as a node label: 2 = names a country, 1 = flag, 0 = generic."""
    if country_from_text(name):
        return 2
    if country_from_flag(name):
        return 1
    return 0


def parse_country_list(raw) -> list[str]:
    """'nl, DE  fi' / ['nl','de'] -> ['NL','DE','FI'] (validated, order kept, de-duplicated)."""
    if isinstance(raw, str):
        items = re.split(r"[\s,;]+", raw)
    else:
        items = list(raw or [])
    out: list[str] = []
    for it in items:
        it = str(it).strip().upper()
        if not it:
            continue
        if it == "UK":
            it = "GB"
        if not re.fullmatch(r"[A-Z]{2}|\?\?", it):
            raise ValueError(f"bad country code: {it!r}")
        if it not in out:
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# Node metadata (sidecar written by compose_xray_config.py)
# ---------------------------------------------------------------------------

META_KEY = "_meta"


def outbound_endpoint(ob: dict) -> tuple[str, int]:
    st = ob.get("settings", {}) or {}
    servers = st.get("vnext") or st.get("servers") or [{}]
    first = servers[0] if servers else {}
    return str(first.get("address", "") or ""), int(first.get("port", 0) or 0)


def build_nodes_meta(outbounds: list[dict], overrides: dict | None = None) -> dict:
    """Collect per-tag metadata from outbounds (reads and does NOT strip _meta)."""
    overrides = overrides or {}
    nodes = {}
    for ob in outbounds:
        tag = ob.get("tag")
        proto = ob.get("protocol")
        if not tag or proto in ("freedom", "blackhole", "dns"):
            continue
        meta = ob.get(META_KEY) or {}
        host, port = outbound_endpoint(ob)
        name = meta.get("name", "") or ""
        iso, src = detect_country(name, meta.get("orig_tag", ""), host)
        if proto == "socks" and tag.startswith("openvpn"):
            name = name or "OpenVPN fallback"
        if tag in overrides:
            iso, src = str(overrides[tag]).upper(), "override"
        nodes[tag] = {
            "name": name,
            "country": iso,
            "country_src": src,
            "host": host,
            "port": port,
            "protocol": proto,
            "source": meta.get("source"),
        }
    return {"generated_at": _dt.datetime.now().isoformat(timespec="seconds"), "nodes": nodes}


def strip_meta(outbounds: list[dict]) -> list[dict]:
    for ob in outbounds:
        ob.pop(META_KEY, None)
    return outbounds


def node_country(tag: str, meta: dict, policy: dict | None = None) -> str:
    ov = ((policy or {}).get("country_overrides") or {}).get(tag)
    if ov:
        return str(ov).upper()
    return ((meta or {}).get("nodes", {}).get(tag) or {}).get("country", "??")


# ---------------------------------------------------------------------------
# Policy (control file)
# ---------------------------------------------------------------------------

POLICY_VERSION = 1
FILTER_MODES = ("all", "only", "except")
SELECT_STRATEGIES = ("best", "next", "random")
MAIN_LANE = "global"


def default_lane() -> dict:
    return {
        "mode": "auto",          # auto: daemon picks; manual: hold `node`
        "node": None,
        "filter": {"mode": "all", "countries": []},
        "rotation": {"schedule": "daily", "strategy": "best"},
        "failover": True,        # manual: leave a dead pinned node (and come back later)
    }


def default_policy() -> dict:
    return {
        "version": POLICY_VERSION,
        "global": default_lane(),
        "clients": {},           # ip -> lane (+ optional "name")
        "aliases": {},           # ip -> friendly name (display only)
        "country_overrides": {}, # tag -> ISO
        "rotate_requests": {},   # lane id -> iso timestamp (one-shot "rotate now")
        "updated_at": None,
    }


def _merge_lane(raw: dict | None, base: dict | None = None) -> dict:
    lane = copy.deepcopy(base or default_lane())
    raw = raw or {}
    for k in ("mode", "node", "failover", "name"):
        if k in raw:
            lane[k] = raw[k]
    if isinstance(raw.get("filter"), dict):
        lane["filter"].update(raw["filter"])
    elif raw.get("filter") is None and "filter" in raw and base is not None:
        pass
    if isinstance(raw.get("rotation"), dict):
        lane["rotation"].update(raw["rotation"])
    lane["filter"]["countries"] = parse_country_list(lane["filter"].get("countries", []))
    if lane["filter"].get("mode") not in FILTER_MODES:
        lane["filter"]["mode"] = "all"
    if lane["rotation"].get("strategy") not in SELECT_STRATEGIES:
        lane["rotation"]["strategy"] = "best"
    if lane.get("mode") not in ("auto", "manual"):
        lane["mode"] = "auto"
    return lane


def normalize_policy(raw: dict | None) -> dict:
    pol = default_policy()
    raw = raw or {}
    pol["global"] = _merge_lane(raw.get("global"))
    for ip, lane in (raw.get("clients") or {}).items():
        pol["clients"][str(ip)] = _merge_lane(lane)
    pol["aliases"] = {str(k): str(v) for k, v in (raw.get("aliases") or {}).items()}
    pol["country_overrides"] = {str(k): str(v).upper() for k, v in (raw.get("country_overrides") or {}).items()}
    pol["rotate_requests"] = dict(raw.get("rotate_requests") or {})
    pol["updated_at"] = raw.get("updated_at")
    return pol


def load_policy(path: Path | str) -> dict:
    p = Path(path)
    if not p.exists():
        return default_policy()
    with open(p, "r", encoding="utf-8") as f:
        return normalize_policy(json.load(f))


def save_policy(path: Path | str, policy: dict) -> None:
    p = Path(path)
    policy = normalize_policy(policy)
    policy["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".policy.", dir=str(p.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(policy, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)


def lane_id_for_client(ip: str) -> str:
    return "client:" + ip


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def allowed_tags(tags: list[str], filt: dict | None, countries: dict[str, str]) -> list[str]:
    filt = filt or {"mode": "all"}
    mode = filt.get("mode", "all")
    cc = set(parse_country_list(filt.get("countries", [])))
    if mode == "only":
        return [t for t in tags if countries.get(t, "??") in cc]
    if mode == "except":
        return [t for t in tags if countries.get(t, "??") not in cc]
    return list(tags)


def alive_sorted(statuses: dict[str, dict], candidates: list[str]) -> list[str]:
    """Alive candidates sorted by delay (statuses: tag -> {alive, delay})."""
    alive = [t for t in candidates if (statuses.get(t) or {}).get("alive") and (statuses.get(t) or {}).get("delay", 0) > 0]
    return sorted(alive, key=lambda t: statuses[t]["delay"])


def pick_best(statuses: dict, candidates: list[str], exclude: str | None = None) -> str | None:
    lst = [t for t in alive_sorted(statuses, candidates) if t != exclude]
    return lst[0] if lst else None


def pick_next(order: list[str], current: str | None, statuses: dict, candidates: list[str]) -> str | None:
    """Next alive candidate after `current` in `order` (wraps around)."""
    cset = set(candidates)
    alive = set(alive_sorted(statuses, candidates))
    if not order:
        return None
    start = order.index(current) + 1 if current in order else 0
    for i in range(len(order)):
        t = order[(start + i) % len(order)]
        if t in cset and t in alive and t != current:
            return t
    return None


def pick_random(statuses: dict, candidates: list[str], exclude: str | None = None, rng=random) -> str | None:
    lst = [t for t in alive_sorted(statuses, candidates) if t != exclude]
    return rng.choice(lst) if lst else None


def pick(strategy: str, order: list[str], current: str | None, statuses: dict,
         candidates: list[str], rng=random) -> str | None:
    if strategy == "next":
        return pick_next(order, current, statuses, candidates)
    if strategy == "random":
        return pick_random(statuses, candidates, exclude=current, rng=rng)
    return pick_best(statuses, candidates)


_INTERVAL_RE = re.compile(r"^(\d+)\s*([mhd])$")


def parse_schedule(schedule: str | None) -> tuple[str, int]:
    """'off' | 'daily' | 'daily@HH' | '<N>m|h|d' -> (kind, value).

    kind: off -> (off, 0); daily -> (daily, hour); interval -> (interval, seconds)
    """
    s = (schedule or "daily").strip().lower()
    if s in ("off", "none", "never", "0"):
        return "off", 0
    if s.startswith("daily"):
        hour = 6
        if "@" in s:
            hour = int(s.split("@", 1)[1])
            if not 0 <= hour <= 23:
                raise ValueError(f"bad hour in schedule {schedule!r}")
        return "daily", hour
    m = _INTERVAL_RE.match(s)
    if not m:
        raise ValueError(f"bad rotation schedule {schedule!r} (off|daily|daily@H|30m|6h|1d)")
    n = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if n < 300:
        raise ValueError("rotation interval must be >= 5m")
    return "interval", n


def next_rotation_time(schedule: str | None, now: _dt.datetime, window_min: int = 30,
                       rng=random) -> _dt.datetime | None:
    kind, val = parse_schedule(schedule)
    if kind == "off":
        return None
    if kind == "interval":
        return now + _dt.timedelta(seconds=val)
    jitter = lambda: rng.randint(-window_min * 60, window_min * 60)  # noqa: E731
    target = now.replace(hour=val, minute=0, second=0, microsecond=0) + _dt.timedelta(seconds=jitter())
    if target <= now:
        base = (now + _dt.timedelta(days=1)).replace(hour=val, minute=0, second=0, microsecond=0)
        target = base + _dt.timedelta(seconds=jitter())
    return target


# ---------------------------------------------------------------------------
# Routing: per-client pins
# ---------------------------------------------------------------------------

MAIN_BALANCER = os.getenv("XRAY_BALANCER_TAG", "proxy-auto")
PIN_RULE_PREFIX = "pin-"
CLIENT_BALANCER_PREFIX = "client-"


def client_balancer_tag(ip: str) -> str:
    return CLIENT_BALANCER_PREFIX + re.sub(r"[^0-9A-Za-z]", "_", ip)


def apply_client_pins(routing: dict, client_ips: list[str], main_balancer: str = MAIN_BALANCER) -> dict:
    """Return a copy of `routing` with one balancer + source rule per pinned client.

    Pins are inserted right before the catch-all main-balancer rule, i.e. after
    the api / direct-bypass rules, so bypass still wins for pinned clients.
    Previously generated pins are replaced. Without a main balancer (single-node
    config) routing is returned unchanged.
    """
    r = copy.deepcopy(routing or {})
    rules = [x for x in r.get("rules", []) if not str(x.get("ruleTag", "")).startswith(PIN_RULE_PREFIX)]
    balancers = [b for b in r.get("balancers", []) if not str(b.get("tag", "")).startswith(CLIENT_BALANCER_PREFIX)]
    main = next((b for b in balancers if b.get("tag") == main_balancer), None)
    if main is None:
        r["rules"], r["balancers"] = rules, balancers
        if not balancers:
            r.pop("balancers", None)
        return r
    idx = next((i for i, x in enumerate(rules) if x.get("balancerTag") == main_balancer), len(rules))
    new_rules, new_bals = [], []
    for ip in sorted(set(client_ips)):
        btag = client_balancer_tag(ip)
        b = copy.deepcopy(main)
        b["tag"] = btag
        new_bals.append(b)
        new_rules.append({"type": "field", "ruleTag": PIN_RULE_PREFIX + ip, "source": [ip], "balancerTag": btag})
    r["rules"] = rules[:idx] + new_rules + rules[idx:]
    r["balancers"] = balancers + new_bals
    return r


def pinned_ips(policy: dict) -> list[str]:
    return sorted((policy or {}).get("clients", {}).keys())


# ---------------------------------------------------------------------------
# Access log -> clients
# ---------------------------------------------------------------------------

_ACCESS_RE = re.compile(
    r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?\s+"
    r"from (?:(?:tcp|udp):)?(?P<ip>\[[0-9a-fA-F:]+\]|[0-9.]+):\d+\s+"
    r"(?P<status>accepted|rejected)\s+"
    r"(?:(?P<net>tcp|udp):)?(?://)?(?P<dest>\S+?)\s+"
    r"\[(?P<route>[^\]]*)\]"
)


def parse_access_line(line: str) -> dict | None:
    m = _ACCESS_RE.match(line.strip())
    if not m:
        return None
    route = m.group("route").strip()
    inbound, outbound = "", route
    for sep in (" >> ", " -> "):
        if sep in route:
            inbound, outbound = route.split(sep, 1)
            break
    dest = m.group("dest")
    host = dest.rsplit(":", 1)[0] if ":" in dest else dest
    ts = _dt.datetime.strptime(m.group("ts"), "%Y/%m/%d %H:%M:%S").replace(tzinfo=_dt.timezone.utc)
    return {
        "ts": ts,
        "ip": m.group("ip").strip("[]"),
        "status": m.group("status"),
        "net": m.group("net") or "",
        "dest": dest,
        "host": host,
        "inbound": inbound.strip(),
        "outbound": outbound.strip(),
    }


class ClientTracker:
    """Incremental aggregation of access-log lines per source IP."""

    IGNORE = {"127.0.0.1", "::1"}

    def __init__(self, top_n: int = 5):
        self.top_n = top_n
        self.clients: dict[str, dict] = {}

    def feed(self, line: str) -> None:
        ev = parse_access_line(line)
        if not ev or ev["ip"] in self.IGNORE:
            return
        c = self.clients.setdefault(ev["ip"], {
            "ip": ev["ip"], "first_seen": ev["ts"], "last_seen": ev["ts"], "conns": 0,
            "outbound": "", "outbounds": {}, "hosts": {}, "inbound": "",
        })
        c["conns"] += 1
        if ev["ts"] >= c["last_seen"]:
            c["last_seen"] = ev["ts"]
            c["outbound"] = ev["outbound"]
            c["inbound"] = ev["inbound"] or c["inbound"]
        c["outbounds"][ev["outbound"]] = c["outbounds"].get(ev["outbound"], 0) + 1
        c["hosts"][ev["host"]] = c["hosts"].get(ev["host"], 0) + 1
        if len(c["hosts"]) > 400:  # bound memory for long-running TUI sessions
            keep = sorted(c["hosts"].items(), key=lambda kv: -kv[1])[:200]
            c["hosts"] = dict(keep)

    def feed_many(self, lines) -> None:
        for ln in lines:
            self.feed(ln)

    def snapshot(self, since: _dt.datetime | None = None) -> list[dict]:
        out = []
        for c in self.clients.values():
            if since and c["last_seen"] < since:
                continue
            d = dict(c)
            d["top_hosts"] = [h for h, _ in sorted(c["hosts"].items(), key=lambda kv: -kv[1])[: self.top_n]]
            out.append(d)
        return sorted(out, key=lambda c: c["last_seen"], reverse=True)


def ip_sort_key(ip: str):
    try:
        return (0, tuple(int(x) for x in ip.split(".")))
    except ValueError:
        return (1, ip)
