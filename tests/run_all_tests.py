#!/usr/bin/env python3
"""
Run all tests for XRAY-PROXY-Container project
"""

import sys
import os
from pathlib import Path

def main():
    """Run all tests using pytest"""
    print("Running all tests for XRAY-PROXY-Container using pytest...\n")
    
    # Change to the project directory regardless of launch location
    project_dir = Path(__file__).resolve().parents[1]
    original_dir = os.getcwd()
    
    try:
        os.chdir(project_dir)
        
        # Run pytest with coverage and warnings disabled for cleaner output
        import subprocess
        result = subprocess.run([
            sys.executable, '-m', 'pytest', 
            'tests/', 
            '-v',
            '--tb=short'
        ], capture_output=True, text=True)
        
        print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)
            
        if result.returncode == 0:
            print("\nAll tests PASSED! The project is ready for use.")
            return 0
        else:
            print(f"\nSome tests FAILED! Return code: {result.returncode}")
            return 1
            
    except Exception as e:
        print(f"Error running tests: {e}")
        return 1
    finally:
        os.chdir(original_dir)

if __name__ == '__main__':
    sys.exit(main())
