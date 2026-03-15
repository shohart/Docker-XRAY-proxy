#!/usr/bin/env python3
"""
Environment variable validation tests for XRAY-PROXY-Container.
"""

import os
import sys


def _read_env_content():
    if os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8") as file:
            return file.read(), ".env"
    with open(".env.example", "r", encoding="utf-8") as file:
        return file.read(), ".env.example"


def _assert_var_present(content: str, var_name: str, source: str) -> None:
    has_plain = f"{var_name}=" in content
    has_spaced = f"{var_name} =" in content
    assert has_plain or has_spaced, (
        f"Required environment variable {var_name} not found in {source}"
    )


def test_env_variables():
    """Test that required vars are present in .env or .env.example."""
    try:
        content, source = _read_env_content()

        required_vars = [
            "XRAY_SUBSCRIPTION_URL",
            "XRAY_IMAGE",
            "SUB_UPDATE_INTERVAL_MIN",
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
            "XRAY_SAVE_RAW_SUBSCRIPTION",
        ]
        optional_control_plane_vars = [
            "THREEX_UI_IMAGE",
            "THREEX_UI_BIND_IP",
            "THREEX_UI_PORT",
        ]

        for var in required_vars:
            _assert_var_present(content, var, source)

        if source == ".env.example":
            for var in optional_control_plane_vars:
                _assert_var_present(content, var, source)

        print(f"OK: All required environment variables are present in {source}")

        for line in content.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("=", 1)
            if len(parts) != 2:
                continue

            var_name = parts[0].strip()
            var_value = parts[1].strip()

            if var_name == "XRAY_SUBSCRIPTION_URL" and var_value in {
                "https://your-provider/subscription",
                "https://your-provider/config.json",
            }:
                print(f"WARNING: Default subscription URL found in {source}")
            elif var_name == "SUB_UPDATE_INTERVAL_MIN" and var_value == "60":
                print(f"INFO: Default update interval found in {source}")

    except Exception as exc:
        raise AssertionError(
            f"Failed to read or validate env file: {exc}"
        ) from exc


def test_env_example():
    """Test that .env.example has required variables."""
    example_path = ".env.example"

    try:
        if not os.path.exists(example_path):
            print("INFO: .env.example does not exist (this is OK for production)")
            return

        with open(example_path, "r", encoding="utf-8") as file:
            content = file.read()

        required_vars = [
            "XRAY_SUBSCRIPTION_URL",
            "XRAY_IMAGE",
            "SUB_UPDATE_INTERVAL_MIN",
            "XRAY_SAVE_RAW_SUBSCRIPTION",
            "THREEX_UI_IMAGE",
            "THREEX_UI_BIND_IP",
            "THREEX_UI_PORT",
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
            _assert_var_present(content, var, ".env.example")

        print("OK: .env.example contains all required variables")

    except Exception as exc:
        raise AssertionError(f"Could not check .env.example: {exc}") from exc


if __name__ == "__main__":
    try:
        test_env_variables()
        test_env_example()
        print("All environment tests PASSED! ✅")
    except Exception as exc:
        print(f"Environment test FAILED: {exc}")
        sys.exit(1)
