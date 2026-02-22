#!/usr/bin/env python3
"""
Environment variable validation tests for XRAY-PROXY-Container
"""

import os
import sys

def test_env_variables():
    """Test that all required environment variables are present in .env file"""
    
    env_path = '.env'
    
    try:
        with open(env_path, 'r') as f:
            content = f.read()
        
        # List of required environment variables
        required_vars = [
            'XRAY_SUBSCRIPTION_URL',
            'SUB_UPDATE_INTERVAL_MIN',
            'LAN_LISTEN_IP',
            'HTTP_PROXY_PORT',
            'SOCKS_PROXY_PORT',
            'LAN_CIDR',
            'GATEWAY_MODE'
        ]
        
        for var in required_vars:
            # Check if variable is defined (either with or without space after =)
            assert f"{var}=" in content or f"{var} =" in content, f"Required environment variable {var} not found"
        
        print("OK: All required environment variables are present")
        
        # Check that some variables have reasonable values
        lines = content.split('\n')
        for line in lines:
            if line.strip() and not line.startswith('#'):
                parts = line.split('=', 1)
                if len(parts) == 2:
                    var_name, var_value = parts[0].strip(), parts[1].strip()
                    if var_name == 'XRAY_SUBSCRIPTION_URL' and var_value == 'https://your-provider/subscription':
                        print("WARNING: Default subscription URL found in .env")
                    elif var_name == 'SUB_UPDATE_INTERVAL_MIN' and var_value == '60':
                        print("INFO: Default update interval found in .env")
        
    except Exception as e:
        raise AssertionError(f"Failed to read or validate .env file: {e}")

def test_env_example():
    """Test that .env.example exists and has the right structure"""
    
    example_path = '.env.example'
    
    try:
        if not os.path.exists(example_path):
            print("INFO: .env.example does not exist (this is OK for production)")
            return
        
        with open(example_path, 'r') as f:
            content = f.read()
        
        # Check that it contains the same variables
        required_vars = [
            'XRAY_SUBSCRIPTION_URL',
            'SUB_UPDATE_INTERVAL_MIN',
            'LAN_LISTEN_IP',
            'HTTP_PROXY_PORT',
            'SOCKS_PROXY_PORT',
            'LAN_CIDR',
            'GATEWAY_MODE'
        ]
        
        for var in required_vars:
            assert f"{var}=" in content or f"{var} =" in content, f"Required environment variable {var} not found in .env.example"
        
        print("OK: .env.example contains all required variables")
        
    except Exception as e:
        raise AssertionError(f"Could not check .env.example: {e}")

if __name__ == '__main__':
    try:
        test_env_variables()
        test_env_example()
        print("All environment tests PASSED! ✅")
    except Exception as e:
        print(f"Environment test FAILED: {e}")
        sys.exit(1)
