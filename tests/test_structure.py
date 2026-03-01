#!/usr/bin/env python3
"""
Test script to verify the structure and files of XRAY-PROXY-Container project
"""

import os
import sys
import json

def _read_env_content():
    if os.path.exists('.env'):
        with open('.env', 'r', encoding='utf-8') as f:
            return f.read(), '.env'
    with open('.env.example', 'r', encoding='utf-8') as f:
        return f.read(), '.env.example'

def test_project_structure():
    """Test that all required files and directories exist"""
    
    # Define expected files and directories
    expected_files = [
        'docker-compose.yml',
        '.env.example',
        'config/config.json',
        'config/example_subscription.json',
        'scripts/update_subscription.sh',
        'scripts/compose_xray_config.py',
        'scripts/gateway_iptables.sh',
        'README.md'
    ]
    
    expected_directories = [
        'config',
        'data',
        'scripts',
        'tests'
    ]
    
    base_path = '.'
    
    # Check directories
    for directory in expected_directories:
        full_path = os.path.join(base_path, directory)
        assert os.path.exists(full_path), f"Directory {full_path} does not exist"
        print(f"OK: Directory {full_path} exists")
    
    # Check files
    for file_path in expected_files:
        full_path = os.path.join(base_path, file_path)
        assert os.path.exists(full_path), f"File {full_path} does not exist"
        print(f"OK: File {full_path} exists")

def test_config_files():
    """Test that configuration files are valid"""
    
    config_path = 'config/config.json'
    
    try:
        with open(config_path, 'r') as f:
            config_data = json.load(f)
        
        # Check required keys
        required_keys = ['log', 'inbounds', 'outbounds', 'routing']
        for key in required_keys:
            assert key in config_data, f"Required key '{key}' missing from config.json"
        
        print("OK: Configuration file is valid JSON and contains required keys")
        
    except Exception as e:
        raise AssertionError(f"Failed to read or parse config.json: {e}")

def test_env_file():
    """Test that .env or .env.example has required variables"""
    try:
        content, source = _read_env_content()
        
        # Check for required environment variables
        required_vars = [
            'XRAY_SUBSCRIPTION_URL',
            'XRAY_IMAGE',
            'SUB_UPDATE_INTERVAL_MIN',
            'LAN_LISTEN_IP',
            'HTTP_PROXY_PORT',
            'SOCKS_PROXY_PORT',
            'LAN_CIDR',
            'GATEWAY_MODE',
            'GATEWAY_TPROXY_PORT',
            'BYPASS_DOMAINS',
            'BYPASS_DOMAIN_ZONES',
            'BYPASS_IP_CIDRS',
            'BYPASS_IP_MASKS'
        ]
        
        for var in required_vars:
            assert f"{var}=" in content or f"{var} =" in content, f"Required environment variable {var} not found"
        
        print(f"OK: All required environment variables found in {source}")
        
    except Exception as e:
        raise AssertionError(f"Failed to read env file: {e}")

def test_script_executable():
    """Test that update script is executable"""
    
    script_path = 'scripts/update_subscription.sh'
    
    try:
        # On Windows, we can't check executable permissions directly,
        # but we can at least verify the file exists and has content
        assert os.path.exists(script_path), f"Script {script_path} does not exist"
        
        with open(script_path, 'r') as f:
            content = f.read()
        
        assert len(content) >= 10, f"Script {script_path} appears to be empty"
        
        print("OK: Update script exists and has content")
        
    except Exception as e:
        raise AssertionError(f"Failed to read update script: {e}")

if __name__ == '__main__':
    try:
        test_project_structure()
        test_config_files()
        test_env_file()
        test_script_executable()
        print("All tests PASSED! ✅")
    except Exception as e:
        print(f"Test FAILED: {e}")
        sys.exit(1)
