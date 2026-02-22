#!/usr/bin/env python3
"""
Configuration validation tests for XRAY-PROXY-Container
"""

import json
import sys
import os

def validate_json_file(file_path, expected_keys=None):
    """Validate that a JSON file is valid and has required keys"""
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
        
        if expected_keys:
            missing_keys = [key for key in expected_keys if key not in data]
            if missing_keys:
                print(f"ERROR: Missing keys in {file_path}: {missing_keys}")
                return False
        
        print(f"OK: {file_path} is valid JSON")
        return True
        
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {file_path}: {e}")
        return False
    except Exception as e:
        print(f"ERROR: Failed to read {file_path}: {e}")
        return False

def test_config_structure():
    """Test the structure of config.json"""
    
    config_path = 'XRAY-PROXY-Container/config/config.json'
    
    # Expected keys for Xray configuration
    expected_keys = ['log', 'inbounds', 'outbounds', 'routing']
    
    # Check basic JSON validity and structure
    if not validate_json_file(config_path, expected_keys):
        return False
    
    # Detailed validation
    try:
        with open(config_path, 'r') as f:
            config_data = json.load(f)
        
        # Validate inbounds structure
        if 'inbounds' not in config_data:
            print("ERROR: Missing 'inbounds' section")
            return False
        
        inbounds = config_data['inbounds']
        if not isinstance(inbounds, list):
            print("ERROR: 'inbounds' should be a list")
            return False
        
        # Check for HTTP and SOCKS5 proxies
        http_found = False
        socks_found = False
        
        for inbound in inbounds:
            if inbound.get('protocol') == 'http':
                http_found = True
            elif inbound.get('protocol') == 'socks':
                socks_found = True
        
        if not http_found:
            print("WARNING: HTTP proxy configuration not found")
        
        if not socks_found:
            print("WARNING: SOCKS5 proxy configuration not found")
        
        # Validate outbounds structure
        if 'outbounds' not in config_data:
            print("ERROR: Missing 'outbounds' section")
            return False
        
        outbounds = config_data['outbounds']
        if not isinstance(outbounds, list):
            print("ERROR: 'outbounds' should be a list")
            return False
        
        print("OK: Configuration structure is valid")
        return True
        
    except Exception as e:
        print(f"ERROR: Failed detailed validation of config.json: {e}")
        return False

def test_example_subscription():
    """Test example subscription file"""
    
    subscription_path = 'XRAY-PROXY-Container/config/example_subscription.json'
    
    expected_keys = ['clients', 'ps', 'v', 'server', 'port']
    
    if not validate_json_file(subscription_path, expected_keys):
        return False
    
    print("OK: Example subscription file is valid")
    return True

def main():
    """Run configuration validation tests"""
    print("Running XRAY-PROXY-Container configuration validation tests...\n")
    
    tests = [
        test_config_structure,
        test_example_subscription
    ]
    
    results = []
    for test in tests:
        print(f"Running {test.__name__}...")
        result = test()
        results.append(result)
        print()
    
    if all(results):
        print("All configuration tests PASSED! ✅")
        return 0
    else:
        print("Some configuration tests FAILED! ❌")
        return 1

if __name__ == '__main__':
    sys.exit(main())