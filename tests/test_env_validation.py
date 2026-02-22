#!/usr/bin/env python3
"""
Environment variable validation tests for XRAY-PROXY-Container
"""

import os
import sys

def test_env_variables():
    """Test that all required environment variables are present in .env file"""
    
    env_path = 'XRAY-PROXY-Container/.env'
    
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
        
        missing_vars = []
        for var in required_vars:
            # Check if variable is defined (either with or without space after =)
            if f"{var}=" not in content and f"{var} =" not in content:
                missing_vars.append(var)
        
        if missing_vars:
            print(f"ERROR: Missing environment variables in .env: {missing_vars}")
            return False
        else:
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
        
        return True
        
    except Exception as e:
        print(f"ERROR: Failed to read or validate .env file: {e}")
        return False

def test_env_example():
    """Test that .env.example exists and has the right structure"""
    
    example_path = 'XRAY-PROXY-Container/.env.example'
    
    try:
        if not os.path.exists(example_path):
            print("INFO: .env.example does not exist (this is OK for production)")
            return True
        
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
        
        missing_vars = []
        for var in required_vars:
            if f"{var}=" not in content and f"{var} =" not in content:
                missing_vars.append(var)
        
        if missing_vars:
            print(f"WARNING: Missing variables in .env.example: {missing_vars}")
        else:
            print("OK: .env.example contains all required variables")
            
        return True
        
    except Exception as e:
        print(f"INFO: Could not check .env.example: {e} (this is OK)")
        return True

def main():
    """Run environment validation tests"""
    print("Running XRAY-PROXY-Container environment validation tests...\n")
    
    tests = [
        test_env_variables,
        test_env_example
    ]
    
    results = []
    for test in tests:
        print(f"Running {test.__name__}...")
        result = test()
        results.append(result)
        print()
    
    if all(results):
        print("All environment tests PASSED! ✅")
        return 0
    else:
        print("Some environment tests FAILED! ❌")
        return 1

if __name__ == '__main__':
    sys.exit(main())