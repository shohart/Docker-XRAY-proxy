import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import geo_probe as gp  # noqa: E402
import tunnel_lib as tl  # noqa: E402


def vless(tag, addr="1.2.3.4", port=443, uid="u1"):
    return {"tag": tag, "protocol": "vless",
            "settings": {"vnext": [{"address": addr, "port": port, "users": [{"id": uid}]}]}}


def test_parse_geo_sources():
    assert gp.parse_geo("cf_trace", "fl=1\nip=45.136.136.45\nloc=NL\ncolo=AMS\n") == ("NL", "45.136.136.45")
    assert gp.parse_geo("cf_trace", "ip=1.1.1.1\nloc=XX\n") == (None, "1.1.1.1")
    assert gp.parse_geo("ipinfo", '{"ip": "1.2.3.4", "country": "de"}') == ("DE", "1.2.3.4")
    assert gp.parse_geo("ipapi", '{"status": "fail"}')[0] is None


def test_endpoint_key_ignores_tag():
    assert gp.endpoint_key(vless("node_1")) == gp.endpoint_key(vless("node_9"))
    assert gp.endpoint_key(vless("a", uid="u1")) != gp.endpoint_key(vless("a", uid="u2"))


def test_probe_config_routes_each_inbound_to_its_node():
    obs = [vless("node_1"), dict(vless("node_2", "5.6.7.8"), _meta={"name": "x"})]
    cfg, mapping = gp.build_probe_config(obs, [20001, 20002])
    assert mapping == {"node_1": 20001, "node_2": 20002}
    assert all(i["listen"] == "127.0.0.1" for i in cfg["inbounds"])
    assert {"type": "field", "inboundTag": ["geo-node_2"], "outboundTag": "node_2"} in cfg["routing"]["rules"]
    assert cfg["routing"]["rules"][-1]["outboundTag"] == "block"          # never direct
    assert "_meta" not in cfg["outbounds"][1] and "_meta" in obs[1]       # input untouched
    assert "api" not in cfg


def test_merge_reindex_survives_renumbering_and_keeps_country_on_failure():
    cache = {"by_key": {}, "nodes": {}}
    obs = [vless("node_1", "a"), vless("node_2", "b")]
    gp.merge_results(cache, obs, {"node_1": {"ok": True, "country": "NL", "ip": "9.9.9.9", "via": "cf_trace"},
                                  "node_2": {"ok": False, "error": "timeout"}})
    assert cache["nodes"] == {"node_1": {"country": "NL", "ip": "9.9.9.9", "checked_at": cache["nodes"]["node_1"]["checked_at"],
                                         "via": "cf_trace"}}
    renamed = [vless("node_7", "a"), vless("node_2", "b")]
    gp.reindex(cache, renamed)
    assert cache["nodes"]["node_7"]["country"] == "NL" and "node_1" not in cache["nodes"]
    gp.merge_results(cache, renamed, {"node_7": {"ok": False, "error": "down"}})
    assert cache["nodes"]["node_7"]["country"] == "NL"                      # last known country kept


def test_due_for_probe_fresh_stale_and_backoff():
    now = dt.datetime(2026, 11, 23, 12, 0, tzinfo=dt.timezone.utc)
    a, b, c, d = vless("a", "a"), vless("b", "b"), vless("c", "c"), vless("d", "d")
    iso = lambda h: (now - dt.timedelta(hours=h)).isoformat()  # noqa: E731
    cache = {"by_key": {
        gp.endpoint_key(a): {"country": "NL", "checked_at": iso(1)},                   # fresh
        gp.endpoint_key(b): {"country": "NL", "checked_at": iso(13)},                  # stale
        gp.endpoint_key(c): {"failed_at": iso(0.25), "failures": 1},                   # 15m ago: wait
    }, "nodes": {}}
    due = {o["tag"] for o in gp.due_for_probe(cache, [a, b, c, d], refresh_h=12, retry_min=30, now=now)}
    assert due == {"b", "d"}
    cache["by_key"][gp.endpoint_key(c)]["failed_at"] = iso(1)
    assert "c" in {o["tag"] for o in gp.due_for_probe(cache, [c], 12, 30, now=now)}


def test_country_priority_exit_beats_name_override_beats_exit():
    meta = {"nodes": {"n": {"country": "RU", "country_src": "flag"}, "m": {"country": "??", "country_src": "unknown"}}}
    geo = {"n": {"country": "NL"}}
    assert tl.node_country_src("n", meta, {}, geo) == ("NL", "exit")
    assert tl.node_country_src("m", meta, {}, geo) == ("??", "unknown")
    assert tl.node_country_src("n", meta, {"country_overrides": {"n": "de"}}, geo) == ("DE", "manual")
    assert tl.node_country_src("n", meta, {}, None) == ("RU", "flag")
