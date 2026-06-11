"""V25 test configuration — add pipeline directory to sys.path."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
