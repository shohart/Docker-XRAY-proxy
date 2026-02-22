#!/usr/bin/env python3
"""
Run all tests for XRAY-PROXY-Container project
"""

import subprocess
import sys
import os

def run_test_script(script_path):
    """Run a test script and return the result"""
    try:
        # Make script executable (on Unix-like systems)
        if os.name != 'nt':  # Not Windows
            os.chmod(script_path, 0o755)
        
        result = subprocess.run([sys.executable, script_path], 
                               capture_output=True, text=True, check=True)
        print(f"✅ {os.path.basename(script_path)}: PASSED")
        if result.stdout:
            print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ {os.path.basename(script_path)}: FAILED")
        print("STDOUT:", e.stdout)
        print("STDERR:", e.stderr)
        return False
    except Exception as e:
        print(f"❌ {os.path.basename(script_path)}: ERROR - {e}")
        return False

def main():
    """Run all tests"""
    print("Running all tests for XRAY-PROXY-Container...\n")
    
    # Get all test scripts in the tests directory
    tests_dir = 'XRAY-PROXY-Container/tests'
    test_scripts = []
    
    try:
        for filename in os.listdir(tests_dir):
            if filename.startswith('test_') and filename.endswith('.py'):
                test_scripts.append(os.path.join(tests_dir, filename))
    except Exception as e:
        print(f"Error finding test scripts: {e}")
        return 1
    
    if not test_scripts:
        print("No test scripts found!")
        return 1
    
    # Sort scripts for consistent order
    test_scripts.sort()
    
    results = []
    for script in test_scripts:
        print(f"Running {os.path.basename(script)}...")
        result = run_test_script(script)
        results.append(result)
        print()
    
    passed = sum(results)
    total = len(results)
    
    print(f"Test Results: {passed}/{total} tests passed")
    
    if all(results):
        print("🎉 All tests PASSED! The project is ready for use.")
        return 0
    else:
        print("💥 Some tests FAILED! Please check the errors above.")
        return 1

if __name__ == '__main__':
    sys.exit(main())