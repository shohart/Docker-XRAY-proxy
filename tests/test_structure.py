#!/usr/bin/env python3
"""
Structure and baseline invariant tests for XRAY-PROXY-Container.
"""

import json
import os
import sys


def _read_env_content():
    if os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8") as file:
            return file.read(), ".env"
    with open(".env.example", "r", encoding="utf-8") as file:
        return file.read(), ".env.example"


def _assert_var_defined(content: str, var_name: str, source: str) -> None:
    has_plain = f"{var_name}=" in content
    has_spaced = f"{var_name} =" in content
    assert has_plain or has_spaced, (
        f"Required environment variable {var_name} not found in {source}"
    )


def test_project_structure():
    """Test that all required files and directories exist."""
    expected_files = [
        "docker-compose.yml",
        ".env.example",
        "config/config.json",
        "config/example_subscription.json",
        "scripts/update_subscription.sh",
        "scripts/compose_xray_config.py",
        "scripts/apply_xray_config.py",
        "scripts/gateway_iptables.sh",
        "README.md",
    ]

    expected_directories = ["config", "data", "scripts", "tests"]

    for directory in expected_directories:
        assert os.path.exists(directory), f"Directory {directory} does not exist"
        print(f"OK: Directory {directory} exists")

    for file_path in expected_files:
        assert os.path.exists(file_path), f"File {file_path} does not exist"
        print(f"OK: File {file_path} exists")


def test_config_files():
    """Test that configuration files are valid."""
    config_path = "config/config.json"

    try:
        with open(config_path, "r", encoding="utf-8") as file:
            config_data = json.load(file)

        required_keys = ["log", "inbounds", "outbounds", "routing"]
        for key in required_keys:
            assert key in config_data, f"Required key '{key}' missing from config.json"

        print("OK: Configuration file is valid JSON and contains required keys")
    except Exception as exc:
        raise AssertionError(f"Failed to read or parse config.json: {exc}") from exc


def test_env_file():
    """Test that .env or .env.example contains baseline variables."""
    try:
        content, source = _read_env_content()

        required_vars = [
            "XRAY_SUBSCRIPTION_URL",
            "XRAY_IMAGE",
            "SUB_UPDATE_INTERVAL_MIN",
            "XRAY_SAVE_RAW_SUBSCRIPTION",
            "LAN_LISTEN_IP",
            "HTTP_PROXY_PORT",
            "SOCKS_PROXY_PORT",
            "LAN_CIDR",
            "GATEWAY_MODE",
            "GATEWAY_TPROXY_PORT",
            "BYPASS_DOMAINS",
            "BYPASS_DOMAIN_ZONES",
            "BYPASS_IP_CIDRS",
            "BYPASS_IP_MASKS",
        ]
        for var in required_vars:
            _assert_var_defined(content, var, source)

        if source == ".env.example":
            control_plane_vars = [
                "THREEX_UI_IMAGE",
                "THREEX_UI_BIND_IP",
                "THREEX_UI_PORT",
            ]
            for var in control_plane_vars:
                _assert_var_defined(content, var, source)

        print(f"OK: All required environment variables found in {source}")
    except Exception as exc:
        raise AssertionError(f"Failed to read env file: {exc}") from exc


def test_scripts_exist_and_have_content():
    """Test that updater/apply scripts exist and are not empty."""
    script_paths = [
        "scripts/update_subscription.sh",
        "scripts/apply_xray_config.py",
    ]

    try:
        for script_path in script_paths:
            assert os.path.exists(script_path), f"Script {script_path} does not exist"

            with open(script_path, "r", encoding="utf-8") as file:
                content = file.read()

            assert len(content) >= 10, f"Script {script_path} appears to be empty"

        print("OK: Required scripts exist and have content")
    except Exception as exc:
        raise AssertionError(f"Failed to validate scripts: {exc}") from exc


if __name__ == "__main__":
    try:
        test_project_structure()
        test_config_files()
        test_env_file()
        test_scripts_exist_and_have_content()
        print("All tests PASSED! ✅")
    except Exception as exc:
        print(f"Test FAILED: {exc}")
        sys.exit(1)
