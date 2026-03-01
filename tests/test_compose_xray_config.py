#!/usr/bin/env python3
"""
Tests for scripts/compose_xray_config.py
"""

import importlib.util
from pathlib import Path

import pytest


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

    default_rule = cfg["routing"]["rules"][-1]
    assert default_rule["balancerTag"] == "proxy-auto"


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
