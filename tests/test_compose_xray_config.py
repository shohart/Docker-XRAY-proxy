#!/usr/bin/env python3
"""
Tests for scripts/compose_xray_config.py
"""

import importlib.util
from pathlib import Path

import pytest


# Clean all XRAY/OpenVPN env vars to prevent host .env from leaking into tests.
_XRAY_ENV_KEYS = [k for k in __import__('os').environ if k.startswith(('XRAY_', 'OPENVPN_', 'GATEWAY_', 'BYPASS_', 'HTTP_PROXY_PORT', 'SOCKS_PROXY_PORT', 'LAN_'))]

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _XRAY_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "compose_xray_config.py"


def _load_compose_module():
    spec = importlib.util.spec_from_file_location("compose_xray_config", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _source_config():
    return {
        "log": {"loglevel": "info"},
        "outbounds": [
            {
                "tag": "node1",
                "protocol": "vless",
                "settings": {"vnext": [{"address": "example.com", "port": 443, "users": []}]},
            }
        ],
    }


def _source_config_two_nodes():
    return {
        "log": {"loglevel": "info"},
        "outbounds": [
            {
                "tag": "node1",
                "protocol": "vless",
                "settings": {"vnext": [{"address": "example.com", "port": 443, "users": []}]},
            },
            {
                "tag": "node2",
                "protocol": "vless",
                "settings": {"vnext": [{"address": "example.org", "port": 443, "users": []}]},
            },
        ],
    }


def test_gateway_mode_adds_tproxy_and_bypass_rules(monkeypatch):
    mod = _load_compose_module()
    monkeypatch.setenv("GATEWAY_MODE", "1")
    monkeypatch.setenv("GATEWAY_TPROXY_PORT", "23456")
    monkeypatch.setenv("BYPASS_DOMAINS", "example.com,api.example.com")
    monkeypatch.setenv("BYPASS_DOMAIN_ZONES", ".corp,local")
    monkeypatch.setenv("BYPASS_IP_CIDRS", "203.0.113.0/24")
    monkeypatch.setenv("BYPASS_IP_MASKS", "198.51.100.*,203.0.*.*")

    cfg = mod.compose_config(_source_config())

    protocols = [i.get("protocol") for i in cfg["inbounds"]]
    assert "dokodemo-door" in protocols

    tproxy = next(i for i in cfg["inbounds"] if i.get("protocol") == "dokodemo-door")
    assert tproxy["port"] == 23456
    assert tproxy["settings"]["network"] == "tcp,udp"
    assert tproxy["listen"] == "0.0.0.0"
    assert tproxy["streamSettings"]["sockopt"]["tproxy"] == "tproxy"

    rules = cfg["routing"]["rules"]
    domain_rule = next(r for r in rules if "domain" in r)
    ip_rule = next(r for r in rules if "ip" in r)

    assert "full:example.com" in domain_rule["domain"]
    assert "full:api.example.com" in domain_rule["domain"]
    assert "domain:corp" in domain_rule["domain"]
    assert "domain:local" in domain_rule["domain"]
    assert "203.0.113.0/24" in ip_rule["ip"]
    assert "198.51.100.0/24" in ip_rule["ip"]
    assert "203.0.0.0/16" in ip_rule["ip"]


def test_gateway_mode_off_has_no_tproxy(monkeypatch):
    mod = _load_compose_module()
    monkeypatch.setenv("GATEWAY_MODE", "0")

    cfg = mod.compose_config(_source_config())

    protocols = [i.get("protocol") for i in cfg["inbounds"]]
    assert "dokodemo-door" not in protocols


def test_single_proxy_adds_default_outbound_rule(monkeypatch):
    mod = _load_compose_module()
    monkeypatch.delenv("GATEWAY_MODE", raising=False)

    cfg = mod.compose_config(_source_config())
    default_rule = cfg["routing"]["rules"][-1]
    assert default_rule["outboundTag"] == "node1"
    assert "balancers" not in cfg["routing"]


def test_multi_proxy_adds_balancer_and_default_balancer_rule(monkeypatch):
    mod = _load_compose_module()
    monkeypatch.delenv("GATEWAY_MODE", raising=False)

    cfg = mod.compose_config(_source_config_two_nodes())
    assert "balancers" in cfg["routing"]
    bal = cfg["routing"]["balancers"][0]
    assert bal["tag"] == "proxy-auto"
    assert bal["selector"] == ["node1", "node2"]
    assert bal["strategy"]["type"] == "random"
    assert bal["fallbackTag"] == "block"

    default_rule = cfg["routing"]["rules"][-1]
    assert default_rule["balancerTag"] == "proxy-auto"
    assert "observatory" in cfg
    assert cfg["observatory"]["subjectSelector"] == ["node1", "node2"]


def test_ovpn_excluded_from_balancer_and_becomes_fallback(monkeypatch):
    mod = _load_compose_module()
    monkeypatch.delenv("GATEWAY_MODE", raising=False)
    monkeypatch.setenv("OPENVPN_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("OPENVPN_OUT_TAG", "openvpn-out")

    src = _source_config_two_nodes()
    cfg = mod.compose_config(src)

    bal = cfg["routing"]["balancers"][0]
    # OVPN must NOT be in the rotation selector
    assert "openvpn-out" not in bal["selector"]
    assert bal["selector"] == ["node1", "node2"]
    # OVPN must be the fallback
    assert bal["fallbackTag"] == "openvpn-out"

    # OVPN must NOT be in observatory (health checked by its own container healthcheck)
    assert "openvpn-out" not in cfg["observatory"]["subjectSelector"]

    # The OVPN outbound itself must still exist
    tags = [o.get("tag") for o in cfg["outbounds"]]
    assert "openvpn-out" in tags


def test_ovpn_disabled_falls_back_to_block(monkeypatch):
    mod = _load_compose_module()
    monkeypatch.delenv("GATEWAY_MODE", raising=False)
    monkeypatch.delenv("OPENVPN_FALLBACK_ENABLED", raising=False)

    cfg = mod.compose_config(_source_config_two_nodes())
    bal = cfg["routing"]["balancers"][0]
    assert bal["fallbackTag"] == "block"


def test_balancer_strategy_from_env(monkeypatch):
    mod = _load_compose_module()
    monkeypatch.setenv("XRAY_BALANCER_STRATEGY", "leastPing")

    cfg = mod.compose_config(_source_config_two_nodes())
    bal = cfg["routing"]["balancers"][0]
    assert bal["strategy"]["type"] == "leastPing"


def test_invalid_balancer_strategy_raises(monkeypatch):
    mod = _load_compose_module()
    monkeypatch.setenv("XRAY_BALANCER_STRATEGY", "invalid-mode")

    with pytest.raises(ValueError):
        mod.compose_config(_source_config_two_nodes())


def test_invalid_bypass_ip_mask_raises(monkeypatch):
    mod = _load_compose_module()
    monkeypatch.setenv("BYPASS_IP_MASKS", "203.*.10.*")

    with pytest.raises(ValueError):
        mod.compose_config(_source_config())


def test_routing_rules_have_effective_fields(monkeypatch):
    mod = _load_compose_module()
    monkeypatch.setenv("GATEWAY_MODE", "1")

    cfg = mod.compose_config(_source_config())

    effective_keys = {"domain", "ip", "port", "network", "protocol", "source", "sourcePort", "user", "inboundTag", "attrs"}
    for rule in cfg["routing"]["rules"]:
        assert rule.get("type") == "field"
        assert any(key in rule for key in effective_keys), f"Rule has no effective fields: {rule}"


def test_bypass_domains_accept_url_and_hostport_formats(monkeypatch):
    mod = _load_compose_module()
    monkeypatch.setenv("BYPASS_DOMAINS", "https://mail.google.com/path,signaler-pa.clients6.google.com:443")
    monkeypatch.setenv("BYPASS_DOMAIN_ZONES", "*.google.com,https://sub.example.org/")

    cfg = mod.compose_config(_source_config())
    domain_rule = next(r for r in cfg["routing"]["rules"] if "domain" in r)

    assert "full:mail.google.com" in domain_rule["domain"]
    assert "full:signaler-pa.clients6.google.com" in domain_rule["domain"]
    assert "domain:google.com" in domain_rule["domain"]
    assert "domain:sub.example.org" in domain_rule["domain"]


def test_existing_direct_outbound_is_preserved():
    mod = _load_compose_module()
    src = {
        "log": {"loglevel": "info"},
        "outbounds": [
            {
                "tag": "node1",
                "protocol": "vless",
                "settings": {"vnext": [{"address": "example.com", "port": 443, "users": []}]},
            },
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
    }

    cfg = mod.compose_config(src)
    tags = [o.get("tag") for o in cfg["outbounds"]]
    assert tags.count("direct") == 1
    assert tags.count("block") == 1


def test_dns_configuration_default_and_custom(monkeypatch):
    mod = _load_compose_module()

    # Default fallback
    cfg = mod.compose_config(_source_config())
    assert "dns" in cfg
    assert cfg["dns"]["servers"] == ["192.168.1.123", "8.8.8.8"]
    assert cfg["dns"]["queryStrategy"] == "UseIPv4"

    # Custom GATEWAY_DNS_SERVER
    monkeypatch.setenv("GATEWAY_DNS_SERVER", "1.1.1.1")
    cfg2 = mod.compose_config(_source_config())
    assert cfg2["dns"]["servers"] == ["1.1.1.1", "8.8.8.8"]

    # Custom XRAY_DNS_SERVERS
    monkeypatch.setenv("XRAY_DNS_SERVERS", "9.9.9.9, 149.112.112.112")
    cfg3 = mod.compose_config(_source_config())
    assert cfg3["dns"]["servers"] == ["9.9.9.9", "149.112.112.112"]

