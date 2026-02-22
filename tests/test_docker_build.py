#!/usr/bin/env python3
"""
Docker build test for XRAY-PROXY-Container project
"""

import os
import sys
import subprocess

def test_dockerfile_exists():
    """Test that Dockerfile exists"""
    dockerfile_path = 'Dockerfile'
    
    assert os.path.exists(dockerfile_path), f"Dockerfile does not exist at {dockerfile_path}"
    
    # Check that it has required content
    try:
        with open(dockerfile_path, 'r') as f:
            content = f.read()
        
        required_lines = [
            'FROM ghcr.io/xtls/xray-core:latest',
            'COPY config/',
            'HEALTHCHECK',
            'EXPOSE 3128 1080'
        ]
        
        for line in required_lines:
            assert line in content, f"Required line '{line}' not found in Dockerfile"
        
        print("OK: Dockerfile exists with required content")
        
    except Exception as e:
        raise AssertionError(f"Failed to read Dockerfile: {e}")

def test_docker_compose_valid():
    """Test that docker-compose.yml is valid and can be parsed"""
    compose_path = 'docker-compose.yml'
    
    assert os.path.exists(compose_path), f"docker-compose.yml does not exist at {compose_path}"
    
    try:
        # Try to parse the YAML file
        import yaml
        
        with open(compose_path, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        assert compose_data is not None, "docker-compose.yml is empty or invalid"
            
        # Check required services exist
        required_services = ['xray', 'updater']
        services = list(compose_data.get('services', {}).keys())
        
        for service in required_services:
            assert service in services, f"Required service '{service}' not found in docker-compose.yml"
        
        print("OK: docker-compose.yml is valid and contains required services")
        
    except ImportError:
        # If yaml module not available, do basic check
        try:
            with open(compose_path, 'r') as f:
                content = f.read()
            
            assert 'services:' in content, "docker-compose.yml missing required 'services' section"
            print("OK: docker-compose.yml appears to be valid")
        except Exception as e:
            raise AssertionError(f"Failed to read docker-compose.yml: {e}")
    except Exception as e:
        raise AssertionError(f"Failed to parse docker-compose.yml: {e}")

def test_docker_build_structure():
    """Test that all required files are in place for Docker build"""
    required_files = [
        'config/config.json',
        'scripts/update_subscription.sh',
        'Dockerfile'
    ]

    base_path = '.'

    for file_path in required_files:
        full_path = os.path.join(base_path, file_path)
        assert os.path.exists(full_path), f"Required file {full_path} not found for Docker build"
    
    print("OK: All required files for Docker build are present")

def test_docker_compose_services():
    """Test that docker-compose services have correct configuration"""
    try:
        import yaml
        
        with open('docker-compose.yml', 'r') as f:
            compose_data = yaml.safe_load(f)
        
        # Check xray service
        xray_service = compose_data.get('services', {}).get('xray', {})
        assert xray_service, "xray service not found in docker-compose.yml"
        
        # Check required configurations for xray service
        required_xray_configs = ['image', 'network_mode', 'volumes']
        for config in required_xray_configs:
            assert config in xray_service, f"Required configuration '{config}' not found in xray service"
        
        print("OK: xray service has required configurations")
        
        # Check updater service
        updater_service = compose_data.get('services', {}).get('updater', {})
        assert updater_service, "updater service not found in docker-compose.yml"
        
        print("OK: Docker Compose services configuration is valid")
        
    except Exception as e:
        raise AssertionError(f"Failed to validate Docker Compose services: {e}")

if __name__ == '__main__':
    try:
        test_dockerfile_exists()
        test_docker_compose_valid()
        test_docker_build_structure()
        test_docker_compose_services()
        print("All Docker build tests PASSED! ✅")
    except Exception as e:
        print(f"Docker build test FAILED: {e}")
        sys.exit(1)
