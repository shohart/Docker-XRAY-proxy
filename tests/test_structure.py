#!/usr/bin/env python3
"""
Test script to verify the structure and files of XRAY-PROXY-Container project
"""

import os
import sys
import json

def test_project_structure():
    """Test that all required files and directories exist"""
    
    # Define expected files and directories
    expected_files = [
        'docker-compose.yml',
        '.env',
        'config/config.json',
        'config/example_subscription.json',
        'scripts/update_subscription.sh',
        'README.md'
    ]
    
    expected_directories = [
        'config',
        'data/logs',
        'scripts',
        'tests'
    ]
    
    base_path = 'XRAY-PROXY-Container'
    
    # Check directories
    for directory in expected_directories:
        full_path = os.path.join(base_path, directory)
        if not os.path.exists(full_path):
            print(f"ERROR: Directory {full_path} does not exist")
            return False
        else:
            print(f"OK: Directory {full_path} exists")
    
    # Check files
    for file_path in expected_files:
        full_path = os.path.join(base_path, file_path)
        if not os.path.exists(full_path):
            print(f"ERROR: File {full_path} does not exist")
            return False
        else:
            print(f"OK: File {full_path} exists")
    
    return True

def test_config_files():
    """Test that configuration files are valid"""
    
    config_path = 'XRAY-PROXY-Container/config/config.json'
    
    try:
        with open(config_path, 'r') as f:
            config_data = json.load(f)
        
        # Check required keys
        required_keys = ['log', 'inbounds', 'outbounds', 'routing']
        for key in required_keys:
            if key not in config_data:
                print(f"ERROR: Required key '{key}' missing from config.json")
                return False
        
        print("OK: Configuration file is valid JSON and contains required keys")
        return True
        
    except Exception as e:
        print(f"ERROR: Failed to read or parse config.json: {e}")
        return False

def test_env_file():
    """Test that .env file has required variables"""
    
    env_path = 'XRAY-PROXY-Container/.env'
    
    try:
        with open(env_path, 'r') as f:
            content = f.read()
        
        # Check for required environment variables
        required_vars = [
            'XRAY_SUBSCRIPTION_URL',
            'SUB_UPDATE_INTERVAL_MIN',
            'LAN_LISTEN_IP',
            'HTTP_PROXY_PORT',
            'SOCKS_PROXY_PORT',
            'LAN_CIDR',
            'GATEWAY_MODE'
        ]
        
        missing_vars = []
        for var in required_vars:
            if f"{var}=" not in content and f"{var} =" not in content:
                missing_vars.append(var)
        
        if missing_vars:
            print(f"ERROR: Missing environment variables: {missing_vars}")
            return False
        else:
            print("OK: All required environment variables found")
            return True
        
    except Exception as e:
        print(f"ERROR: Failed to read .env file: {e}")
        return False

def test_script_executable():
    """Test that update script is executable"""
    
    script_path = 'XRAY-PROXY-Container/scripts/update_subscription.sh'
    
    try:
        # On Windows, we can't check executable permissions directly,
        # but we can at least verify the file exists and has content
        if not os.path.exists(script_path):
            print(f"ERROR: Script {script_path} does not exist")
            return False
        
        with open(script_path, 'r') as f:
            content = f.read()
        
        if len(content) < 10:  # Very basic check
            print(f"ERROR: Script {script_path} appears to be empty")
            return False
        
        print("OK: Update script exists and has content")
        return True
        
    except Exception as e:
        print(f"ERROR: Failed to read update script: {e}")
        return False

def main():
    """Run all tests"""
    print("Running XRAY-PROXY-Container tests...\n")
    
    tests = [
        test_project_structure,
        test_config_files,
        test_env_file,
        test_script_executable
    ]
    
    results = []
    for test in tests:
        print(f"Running {test.__name__}...")
        result = test()
        results.append(result)
        print()
    
    if all(results):
        print("All tests PASSED! ✅")
        return 0
    else:
        print("Some tests FAILED! ❌")
        return 1

if __name__ == '__main__':
    sys.exit(main())