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
                raise AssertionError(f"Missing keys in {file_path}: {missing_keys}")
        
        print(f"OK: {file_path} is valid JSON")
        
    except json.JSONDecodeError as e:
        raise AssertionError(f"Invalid JSON in {file_path}: {e}")
    except Exception as e:
        raise AssertionError(f"Failed to read {file_path}: {e}")

def test_config_structure():
    """Test the structure of config.json"""
    
    config_path = 'XRAY-PROXY-Container/config/config.json'
    
    # Expected keys for Xray configuration
    expected_keys = ['log', 'inbounds', 'outbounds', 'routing']
    
    # Check basic JSON validity and structure
    validate_json_file(config_path, expected_keys)
    
    # Detailed validation
    try:
        with open(config_path, 'r') as f:
            config_data = json.load(f)
        
        # Validate inbounds structure
        assert 'inbounds' in config_data, "Missing 'inbounds' section"
        
        inbounds = config_data['inbounds']
        assert isinstance(inbounds, list), "'inbounds' should be a list"
        
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
        assert 'outbounds' in config_data, "Missing 'outbounds' section"
        
        outbounds = config_data['outbounds']
        assert isinstance(outbounds, list), "'outbounds' should be a list"
        
        print("OK: Configuration structure is valid")
        
    except Exception as e:
        raise AssertionError(f"Failed detailed validation of config.json: {e}")

def test_example_subscription():
    """Test example subscription file"""
    
    subscription_path = 'XRAY-PROXY-Container/config/example_subscription.json'
    
    expected_keys = ['clients', 'ps', 'v', 'server', 'port']
    
    validate_json_file(subscription_path, expected_keys)
    
    print("OK: Example subscription file is valid")

if __name__ == '__main__':
    try:
        test_config_structure()
        test_example_subscription()
        print("All configuration tests PASSED! ✅")
    except Exception as e:
        print(f"Configuration test FAILED: {e}")
        sys.exit(1)
