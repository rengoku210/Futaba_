"""
Pytest configuration for FUTABA test suite.
Ensures test runs are completely isolated from production storage.
"""

import os
import sys

# Mark the environment as test so that TaskManager defaults to test origin
# and uses the isolated test_tasks directory
os.environ["FUTABA_ENV"] = "test"
