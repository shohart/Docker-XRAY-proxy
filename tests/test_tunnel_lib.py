import datetime
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import tunnel_lib as tl  # noqa: E402


# --------------------------------------------------------------------- countries
@pytest.mark.parametrize("name,hints,iso,src", [
    ("GhostLine 🇪🇪 Estonia", (), "EE", "name"),
    ("🇳🇱 Нидерланды | Запасное", (), "NL", "name"),
    ("🇷🇺 МОСТ Netherlands 2 | vless", (), "NL", "name"),     # bridge: exit country wins over flag
    ("🇷🇺 Обход глушилок #YC - XHTTP", (), "RU", "flag"),
    ("🇵🇱 VIP 👑 Poland | Warsaw", (), "PL", "name"),
    ("🇩🇪 Германия", (), "DE", "name"),
    ("GhostLine Auto", ("ams_1",), "NL", "hint"),
    ("", ("bridge-nl2.slatebridgesystems.blog",), "NL", "hint"),
    ("", ("up9.greenleafyoga.live",), "??", "unknown"),
    ("🚀 AUTO #1 | Весь интернет + LTE", (), "??", "unknown"),
    ("", ("easyweather26.argonaft1.ru",), "??", "unknown"),       # TLD is ignored
])
def test_detect_country(name, hints, iso, src):
    assert tl.detect_country(name, *hints) == (iso, src)


def test_flag_roundtrip():
    assert tl.flag("nl") == "🇳🇱"
    assert tl.country_from_flag(tl.flag("DE")) == "DE"
    assert tl.flag("??") == "🌐"


def test_parse_country_list():
    assert tl.parse_country_list("nl, de  uk") == ["NL", "DE", "GB"]
    assert tl.parse_country_list(["NL", "nl"]) == ["NL"]
    with pytest.raises(ValueError):
        tl.parse_country_list("netherlands")


def test_remark_specificity_prefers_country_names():
    assert tl.remark_specificity("🇳🇱 Netherlands 1") > tl.remark_specificity("🚀 AUTO #1")


# --------------------------------------------------------------------- selection
TAGS = ["a", "b", "c", "d"]
CC = {"a": "NL", "b": "DE", "c": "NL", "d": "RU"}
ST = {"a": {"alive": True, "delay": 300}, "b": {"alive": True, "delay": 100},
      "c": {"alive": False, "delay": 0}, "d": {"alive": True, "delay": 50}}


def test_allowed_tags_modes():
    assert tl.allowed_tags(TAGS, {"mode": "all"}, CC) == TAGS
    assert tl.allowed_tags(TAGS, {"mode": "only", "countries": ["NL"]}, CC) == ["a", "c"]
    assert tl.allowed_tags(TAGS, {"mode": "except", "countries": ["RU", "DE"]}, CC) == ["a", "c"]


def test_pick_strategies():
    allowed = tl.allowed_tags(TAGS, {"mode": "except", "countries": ["RU"]}, CC)
    assert tl.pick_best(ST, allowed) == "b"
    assert tl.pick_next(TAGS, "a", ST, allowed) == "b"
    assert tl.pick_next(TAGS, "b", ST, allowed) == "a"      # c dead, d excluded, wraps
    assert tl.pick_next(TAGS, "zz", ST, allowed) == "a"     # unknown current -> first
    assert tl.pick_random(ST, allowed, exclude="a") == "b"


def test_schedules():
    now = datetime.datetime(2026, 11, 23, 12, 0)
    assert tl.parse_schedule("off") == ("off", 0)
    assert tl.parse_schedule("daily@5") == ("daily", 5)
    assert tl.parse_schedule("6h") == ("interval", 6 * 3600)
    assert tl.next_rotation_time("off", now) is None
    assert tl.next_rotation_time("30m", now) == now + datetime.timedelta(minutes=30)
    nxt = tl.next_rotation_time("daily@5", now)
    assert nxt.day == 24 and 4 <= nxt.hour <= 5
    with pytest.raises(ValueError):
        tl.parse_schedule("1m")
    with pytest.raises(ValueError):
        tl.parse_schedule("weekly")


# --------------------------------------------------------------------- policy
def test_policy_roundtrip(tmp_path):
    p = tmp_path / "tunnel_policy.json"
    assert tl.load_policy(p)["global"]["mode"] == "auto"
    pol = tl.default_policy()
    pol["global"]["filter"] = {"mode": "only", "countries": ["nl", "de"]}
    pol["clients"]["192.168.1.5"] = {"mode": "manual", "node": "node_3"}
    tl.save_policy(p, pol)
    back = tl.load_policy(p)
    assert back["global"]["filter"]["countries"] == ["NL", "DE"]
    c = back["clients"]["192.168.1.5"]
    assert c["mode"] == "manual" and c["node"] == "node_3"
    assert c["rotation"]["schedule"] == "daily"      # defaults filled in


# --------------------------------------------------------------------- routing
ROUTING = {
    "rules": [
        {"type": "field", "inboundTag": ["api-in"], "outboundTag": "api"},
        {"type": "field", "domain": ["domain:ru"], "outboundTag": "direct"},
        {"type": "field", "network": "tcp,udp", "balancerTag": "proxy-auto"},
    ],
    "balancers": [{"tag": "proxy-auto", "selector": ["a", "b"], "fallbackTag": "block",
                   "strategy": {"type": "leastPing"}}],
}


def test_apply_client_pins_order_and_idempotence():
    r = tl.apply_client_pins(ROUTING, ["192.168.1.9", "192.168.1.5"])
    tags = [x.get("ruleTag") for x in r["rules"]]
    assert tags == [None, None, "pin-192.168.1.5", "pin-192.168.1.9", None]
    assert r["rules"][-1]["balancerTag"] == "proxy-auto"           # catch-all stays last
    assert r["rules"][2]["balancerTag"] == "client-192_168_1_5"
    assert [b["tag"] for b in r["balancers"]] == ["proxy-auto", "client-192_168_1_5", "client-192_168_1_9"]
    assert r["balancers"][1]["selector"] == ["a", "b"]
    again = tl.apply_client_pins(r, ["192.168.1.5"])
    assert [x.get("ruleTag") for x in again["rules"]].count("pin-192.168.1.9") == 0
    assert len(again["balancers"]) == 2
    assert tl.apply_client_pins(r, []) == ROUTING
    assert ROUTING["rules"][2] == {"type": "field", "network": "tcp,udp", "balancerTag": "proxy-auto"}


def test_apply_client_pins_without_balancer():
    r = {"rules": [{"type": "field", "network": "tcp,udp", "outboundTag": "node1"}]}
    assert tl.apply_client_pins(r, ["192.168.1.5"]) == r


# --------------------------------------------------------------------- access log
def test_access_log_parsing_and_tracker():
    lines = [
        "2026/11/22 20:53:02.981203 from 192.168.1.246:65068 accepted //beacons4.gvt2.com:443 [node_2_2]",
        "2026/11/22 20:53:03.000000 from tcp:192.168.1.5:5 accepted tcp:x.com:443 [tproxy-in >> node_3]",
        "2026/11/22 20:53:04.000000 from 192.168.1.246:1 accepted udp:claude.ai:443 [tproxy-in -> direct]",
        "2026/11/22 20:53:05.000000 from 127.0.0.1:1 accepted tcp:127.0.0.1:10085 [api]",
        "2026/11/22 20:53:05.000000 [Info] app/dispatcher: taking detour [node_2_2] for [tcp:a:443]",
    ]
    ev = tl.parse_access_line(lines[1])
    assert (ev["ip"], ev["inbound"], ev["outbound"], ev["host"]) == ("192.168.1.5", "tproxy-in", "node_3", "x.com")
    t = tl.ClientTracker()
    t.feed_many(lines)
    snap = {c["ip"]: c for c in t.snapshot()}
    assert set(snap) == {"192.168.1.246", "192.168.1.5"}
    assert snap["192.168.1.246"]["conns"] == 2
    assert snap["192.168.1.246"]["outbound"] == "direct"   # latest wins


# --------------------------------------------------------------------- meta
def test_build_nodes_meta_and_strip():
    obs = [
        {"tag": "node_1", "protocol": "vless", "_meta": {"name": "🇩🇪 Germany 8", "source": 2},
         "settings": {"vnext": [{"address": "1.2.3.4", "port": 443}]}},
        {"tag": "node_2", "protocol": "vless", "_meta": {"name": "x", "orig_tag": "hel_1"},
         "settings": {"vnext": [{"address": "h.example", "port": 8443}]}},
        {"tag": "direct", "protocol": "freedom"},
    ]
    meta = tl.build_nodes_meta(obs, {"node_2": "se"})
    assert meta["nodes"]["node_1"]["country"] == "DE"
    assert meta["nodes"]["node_1"]["source"] == 2
    assert meta["nodes"]["node_2"]["country"] == "SE" and meta["nodes"]["node_2"]["country_src"] == "override"
    assert "direct" not in meta["nodes"]
    tl.strip_meta(obs)
    assert all("_meta" not in o for o in obs)
    json.dumps(meta, ensure_ascii=False)


# --------------------------------------------------------------------- compose integration
def test_compose_bakes_pins_and_strips_meta(monkeypatch):
    import importlib.util
    for k in [k for k in __import__("os").environ if k.startswith(("XRAY_", "OPENVPN_", "GATEWAY_"))]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XRAY_BALANCER_STRATEGY", "leastPing")
    spec = importlib.util.spec_from_file_location("compose_xray_config", REPO_ROOT / "scripts" / "compose_xray_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    vless = lambda tag, name: {"tag": tag, "protocol": "vless", "_meta": {"name": name},  # noqa: E731
                               "settings": {"vnext": [{"address": f"{tag}.example", "port": 443,
                                                       "users": [{"id": "x"}]}]}}
    src = {"outbounds": [vless("node_1", "🇳🇱 Netherlands 1"), vless("node_2", "🇩🇪 Germany 8")]}
    pol = tl.default_policy()
    pol["clients"]["192.168.1.50"] = tl.default_lane()
    cfg, meta = mod.compose_config_with_meta(src, pol)
    assert all("_meta" not in o for o in cfg["outbounds"])
    assert "_meta" in src["outbounds"][0]                      # source not mutated
    assert meta["nodes"]["node_2"]["country"] == "DE"
    assert any(r.get("ruleTag") == "pin-192.168.1.50" for r in cfg["routing"]["rules"])
    assert cfg["routing"]["rules"][-1].get("balancerTag") == "proxy-auto"
    assert {b["tag"] for b in cfg["routing"]["balancers"]} == {"proxy-auto", "client-192_168_1_50"}
