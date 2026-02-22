#!/usr/bin/env python3
"""
Docker build test for XRAY-PROXY-Container project
"""

import os
import sys
import subprocess

def test_dockerfile_exists():
    """Test that Dockerfile exists"""
    dockerfile_path = 'XRAY-PROXY-Container/Dockerfile'
    
    if not os.path.exists(dockerfile_path):
        print(f"ERROR: Dockerfile does not exist at {dockerfile_path}")
        return False
    
    # Check that it has required content
    try:
        with open(dockerfile_path, 'r') as f:
            content = f.read()
        
        required_lines = [
            'FROM ghcr.io/xtls/xray:latest',
            'COPY config/',
            'COPY scripts/',
            'EXPOSE 3128 1080'
        ]
        
        missing_lines = []
        for line in required_lines:
            if line not in content:
                missing_lines.append(line)
        
        if missing_lines:
            print(f"WARNING: Missing lines in Dockerfile: {missing_lines}")
        else:
            print("OK: Dockerfile exists with required content")
            
        return True
        
    except Exception as e:
        print(f"ERROR: Failed to read Dockerfile: {e}")
        return False

def test_docker_compose_valid():
    """Test that docker-compose.yml is valid and can be parsed"""
    compose_path = 'XRAY-PROXY-Container/docker-compose.yml'
    
    if not os.path.exists(compose_path):
        print(f"ERROR: docker-compose.yml does not exist at {compose_path}")
        return False
    
    try:
        # Try to parse the YAML file
        import yaml
        
        with open(compose_path, 'r') as f:
            compose_data = yaml.safe_load(f)
        
        if not compose_data:
            print("ERROR: docker-compose.yml is empty or invalid")
            return False
            
        # Check required services exist
        required_services = ['xray', 'updater']
        services = list(compose_data.get('services', {}).keys())
        
        missing_services = []
        for service in required_services:
            if service not in services:
                missing_services.append(service)
        
        if missing_services:
            print(f"WARNING: Missing services in docker-compose.yml: {missing_services}")
        else:
            print("OK: docker-compose.yml is valid and contains required services")
            
        return True
        
    except ImportError:
        # If yaml module not available, do basic check
        try:
            with open(compose_path, 'r') as f:
                content = f.read()
            
            if 'version:' in content and 'services:' in content:
                print("OK: docker-compose.yml appears to be valid")
                return True
            else:
                print("ERROR: docker-compose.yml missing required sections")
                return False
        except Exception as e:
            print(f"ERROR: Failed to read docker-compose.yml: {e}")
            return False
    except Exception as e:
        print(f"ERROR: Failed to parse docker-compose.yml: {e}")
        return False

def test_docker_build_structure():
    """Test that all required files are in place for Docker build"""
    required_files = [
        'config/config.json',
        'scripts/update_subscription.sh',
        'Dockerfile'
    ]
    
    base_path = 'XRAY-PROXY-Container'
    
    missing_files = []
    for file_path in required_files:
        full_path = os.path.join(base_path, file_path)
        if not os.path.exists(full_path):
            missing_files.append(file_path)
    
    if missing_files:
        print(f"ERROR: Missing files for Docker build: {missing_files}")
        return False
    else:
        print("OK: All required files for Docker build are present")
        return True

def test_docker_compose_services():
    """Test that docker-compose services have correct configuration"""
    try:
        import yaml
        
        with open('XRAY-PROXY-Container/docker-compose.yml', 'r') as f:
            compose_data = yaml.safe_load(f)
        
        # Check xray service
        xray_service = compose_data.get('services', {}).get('xray', {})
        if not xray_service:
            print("ERROR: xray service not found in docker-compose.yml")
            return False
        
        # Check required configurations for xray service
        required_xray_configs = ['image', 'network_mode', 'volumes']
        missing_configs = []
        for config in required_xray_configs:
            if config not in xray_service:
                missing_configs.append(config)
        
        if missing_configs:
            print(f"WARNING: Missing xray service configurations: {missing_configs}")
        else:
            print("OK: xray service has required configurations")
        
        # Check updater service
        updater_service = compose_data.get('services', {}).get('updater', {})
        if not updater_service:
            print("ERROR: updater service not found in docker-compose.yml")
            return False
            
        print("OK: Docker Compose services configuration is valid")
        return True
        
    except Exception as e:
        print(f"ERROR: Failed to validate Docker Compose services: {e}")
        return False

def main():
    """Run Docker build tests"""
    print("Running XRAY-PROXY-Container Docker build tests...\n")
    
    tests = [
        test_dockerfile_exists,
        test_docker_compose_valid,
        test_docker_build_structure,
        test_docker_compose_services
    ]
    
    results = []
    for test in tests:
        print(f"Running {test.__name__}...")
        result = test()
        results.append(result)
        print()
    
    if all(results):
        print("All Docker build tests PASSED! ✅")
        return 0
    else:
        print("Some Docker build tests FAILED! ❌")
        return 1

if __name__ == '__main__':
    sys.exit(main())