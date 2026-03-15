#!/usr/bin/env python3
"""
Docker build and compose tests for XRAY-PROXY-Container.
"""

import os
import sys


def test_dockerfile_exists():
    """Test that Dockerfile exists and has expected baseline markers."""
    dockerfile_path = "Dockerfile"
    assert os.path.exists(
        dockerfile_path
    ), f"Dockerfile does not exist at {dockerfile_path}"

    try:
        with open(dockerfile_path, "r", encoding="utf-8") as file:
            content = file.read()

        required_lines = [
            "FROM ${XRAY_IMAGE}",
            "COPY config/",
            "HEALTHCHECK",
            "EXPOSE 3128 1080",
        ]
        for line in required_lines:
            assert line in content, f"Required line '{line}' not found in Dockerfile"

        print("OK: Dockerfile exists with required content")
    except Exception as exc:
        raise AssertionError(f"Failed to read Dockerfile: {exc}") from exc


def test_docker_compose_valid():
    """Test that docker-compose.yml is valid and has required services."""
    compose_path = "docker-compose.yml"
    assert os.path.exists(
        compose_path
    ), f"docker-compose.yml does not exist at {compose_path}"

    try:
        import yaml

        with open(compose_path, "r", encoding="utf-8") as file:
            compose_data = yaml.safe_load(file)

        assert compose_data is not None, "docker-compose.yml is empty or invalid"
        services = list(compose_data.get("services", {}).keys())
        required_services = ["xray", "updater", "gateway", "xui"]
        for service in required_services:
            assert (
                service in services
            ), f"Required service '{service}' not found in docker-compose.yml"

        print("OK: docker-compose.yml is valid and contains required services")
    except ImportError:
        try:
            with open(compose_path, "r", encoding="utf-8") as file:
                content = file.read()

            assert "services:" in content, (
                "docker-compose.yml missing required 'services' section"
            )
            for marker in ["xray:", "updater:", "gateway:", "xui:"]:
                assert marker in content, f"Service marker '{marker}' not found"
            assert "control-plane" in content, (
                "Expected isolated control-plane network marker not found"
            )

            print(
                "OK: docker-compose.yml fallback validation passed without PyYAML"
            )
        except Exception as exc:
            raise AssertionError(f"Failed to read docker-compose.yml: {exc}") from exc
    except Exception as exc:
        raise AssertionError(f"Failed to parse docker-compose.yml: {exc}") from exc


def test_docker_build_structure():
    """Test that required files for image/runtime are present."""
    required_files = [
        "config/config.json",
        "scripts/update_subscription.sh",
        "scripts/apply_xray_config.py",
        "Dockerfile",
    ]

    for file_path in required_files:
        assert os.path.exists(
            file_path
        ), f"Required file {file_path} not found for Docker build"

    print("OK: All required files for Docker build are present")


def test_docker_compose_services():
    """Test compose service-level security and compatibility invariants."""
    try:
        import yaml
    except ImportError:
        with open("docker-compose.yml", "r", encoding="utf-8") as file:
            content = file.read()

        for marker in ["xray:", "updater:", "gateway:", "xui:"]:
            assert marker in content, f"Service marker '{marker}' not found"
        assert 'network_mode: "host"' in content, (
            "Expected host networking marker for xray/gateway not found"
        )
        assert "control-plane" in content, (
            "Expected isolated network marker not found"
        )
        assert "cap_drop" in content, (
            "Expected capability drop marker for xui not found"
        )
        print("OK: Docker Compose services are present (fallback mode)")
        return

    with open("docker-compose.yml", "r", encoding="utf-8") as file:
        compose_data = yaml.safe_load(file)

    services = compose_data.get("services", {})
    for service_name in ["xray", "updater", "gateway", "xui"]:
        assert (
            service_name in services
        ), f"{service_name} service not found in docker-compose.yml"

    xray_service = services["xray"]
    for config in ["image", "network_mode", "volumes"]:
        assert (
            config in xray_service
        ), f"Required configuration '{config}' not found in xray service"

    xui_service = services["xui"]
    assert "network_mode" not in xui_service, "xui must not use host networking"
    assert "env_file" not in xui_service, "xui must not receive full project .env"

    cap_add = xui_service.get("cap_add", [])
    assert "NET_ADMIN" not in cap_add, "xui must not request NET_ADMIN"
    assert "NET_RAW" not in cap_add, "xui must not request NET_RAW"

    cap_drop = xui_service.get("cap_drop", [])
    assert "ALL" in cap_drop, "xui must drop all capabilities"

    security_opt = xui_service.get("security_opt", [])
    assert (
        "no-new-privileges:true" in security_opt
    ), "xui must run with no-new-privileges"

    networks = xui_service.get("networks", [])
    assert "control-plane" in networks, "xui must be isolated in control-plane network"

    assert "healthcheck" in xui_service, "xui must define a healthcheck"

    ports = xui_service.get("ports", [])
    assert ports, "xui must expose admin panel port mapping"
    assert any(":2053/tcp" in str(port) for port in ports), (
        "xui admin panel must publish tcp/2053"
    )

    print("OK: Docker Compose services configuration is valid")


if __name__ == "__main__":
    try:
        test_dockerfile_exists()
        test_docker_compose_valid()
        test_docker_build_structure()
        test_docker_compose_services()
        print("All Docker build tests PASSED! ✅")
    except Exception as exc:
        print(f"Docker build test FAILED: {exc}")
        sys.exit(1)
