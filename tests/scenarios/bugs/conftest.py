"""
Fixtures for regression test scenarios.
"""

import pytest
import json
import os


@pytest.fixture
def load_scenario(request):
    """Load a scenario JSON file by name."""
    def _load(scenario_name):
        scenario_path = os.path.join(
            os.path.dirname(__file__),
            f"{scenario_name}.json"
        )
        if not os.path.exists(scenario_path):
            raise FileNotFoundError(f"Scenario file not found: {scenario_path}")
        
        with open(scenario_path, 'r') as f:
            return json.load(f)
    
    return _load


@pytest.fixture
def issue_tracker():
    """Track issue metadata for test reporting."""
    class IssueTracker:
        def __init__(self):
            self.issues = {}
        
        def register(self, issue_number, title, severity="medium"):
            """Register an issue being tested."""
            self.issues[issue_number] = {
                'title': title,
                'severity': severity,
                'tested': True
            }
        
        def get_report(self):
            """Get summary of tested issues."""
            return self.issues
    
    return IssueTracker()
