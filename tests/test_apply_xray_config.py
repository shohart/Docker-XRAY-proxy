#!/usr/bin/env python3
"""
Tests for scripts/apply_xray_config.py
"""

import importlib.util
import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "apply_xray_config.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("apply_xray_config", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _valid_config():
    return {
        "log": {"loglevel": "info"},
        "inbounds": [
            {
                "port": 1080,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
            }
        ],
        "outbounds": [
            {
                "tag": "node1",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {"address": "example.com", "port": 443, "users": []}
                    ]
                },
            },
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            {"tag": "block", "protocol": "blackhole", "settings": {}},
        ],
        "routing": {
            "domainStrategy": "IPOnDemand",
            "rules": [
                {"type": "field", "network": "tcp,udp", "outboundTag": "node1"}
            ],
        },
    }


def test_validate_candidate_config_accepts_valid_payload():
    mod = _load_module()
    mod.validate_candidate_config(_valid_config())


def test_validate_candidate_config_requires_block_outbound():
    mod = _load_module()
    cfg = _valid_config()
    cfg["outbounds"] = [ob for ob in cfg["outbounds"] if ob.get("tag") != "block"]

    with pytest.raises(ValueError, match="block"):
        mod.validate_candidate_config(cfg)


def test_validate_candidate_config_requires_proxy_outbound():
    mod = _load_module()
    cfg = _valid_config()
    cfg["outbounds"] = [
        {"tag": "direct", "protocol": "freedom", "settings": {}},
        {"tag": "block", "protocol": "blackhole", "settings": {}},
    ]
    cfg["routing"]["rules"] = [
        {"type": "field", "network": "tcp,udp", "outboundTag": "direct"}
    ]

    with pytest.raises(ValueError, match="proxy outbounds"):
        mod.validate_candidate_config(cfg)


def test_validate_candidate_config_rejects_unknown_outbound_reference():
    mod = _load_module()
    cfg = _valid_config()
    cfg["routing"]["rules"] = [
        {"type": "field", "network": "tcp,udp", "outboundTag": "missing-node"}
    ]

    with pytest.raises(ValueError, match="unknown outboundTag"):
        mod.validate_candidate_config(cfg)


def test_validate_candidate_config_accepts_known_balancer_reference():
    mod = _load_module()
    cfg = _valid_config()
    cfg["routing"]["balancers"] = [{"tag": "proxy-auto", "selector": ["node1"]}]
    cfg["routing"]["rules"] = [
        {"type": "field", "network": "tcp,udp", "balancerTag": "proxy-auto"}
    ]

    mod.validate_candidate_config(cfg)


def test_validate_candidate_config_rejects_unknown_balancer_reference():
    mod = _load_module()
    cfg = _valid_config()
    cfg["routing"]["balancers"] = [{"tag": "proxy-auto", "selector": ["node1"]}]
    cfg["routing"]["rules"] = [
        {"type": "field", "network": "tcp,udp", "balancerTag": "missing-balancer"}
    ]

    with pytest.raises(ValueError, match="unknown balancerTag"):
        mod.validate_candidate_config(cfg)


def test_validate_candidate_config_rejects_rule_without_target():
    mod = _load_module()
    cfg = _valid_config()
    cfg["routing"]["rules"] = [{"type": "field", "network": "tcp,udp"}]

    with pytest.raises(ValueError, match="must define outboundTag or balancerTag"):
        mod.validate_candidate_config(cfg)


def test_validate_candidate_config_rejects_duplicate_outbound_tags():
    mod = _load_module()
    cfg = _valid_config()
    cfg["outbounds"].append(
        {"tag": "node1", "protocol": "vmess", "settings": {"vnext": []}}
    )

    with pytest.raises(ValueError, match="duplicate outbound tags"):
        mod.validate_candidate_config(cfg)


def test_apply_candidate_writes_target_file(tmp_path):
    mod = _load_module()
    candidate_path = tmp_path / "candidate.json"
    target_path = tmp_path / "config.json"

    candidate_path.write_text(
        json.dumps(_valid_config(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    changed = mod.apply_candidate(candidate_path, target_path)
    assert changed is True

    applied = json.loads(target_path.read_text(encoding="utf-8"))
    default_rule = applied["routing"]["rules"][-1]
    assert default_rule["outboundTag"] == "node1"


def test_apply_candidate_returns_false_for_identical_payload(tmp_path):
    mod = _load_module()
    candidate_path = tmp_path / "candidate.json"
    target_path = tmp_path / "config.json"

    payload = json.dumps(_valid_config(), ensure_ascii=False, indent=2)
    candidate_path.write_text(payload, encoding="utf-8")
    target_path.write_text(payload, encoding="utf-8")

    changed = mod.apply_candidate(candidate_path, target_path)
    assert changed is False


def test_apply_candidate_rejects_invalid_json(tmp_path):
    mod = _load_module()
    candidate_path = tmp_path / "candidate.json"
    target_path = tmp_path / "config.json"
    candidate_path.write_text("{bad json", encoding="utf-8")

    with pytest.raises(ValueError, match="valid JSON"):
        mod.apply_candidate(candidate_path, target_path)