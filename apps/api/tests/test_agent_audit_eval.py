"""The offline scripted audit cases also run in the normal regression suite."""
import importlib.util
from pathlib import Path
import sys
import pytest

path = Path(__file__).resolve().parents[3] / "tools" / "audit_agent_eval.py"
spec = importlib.util.spec_from_file_location("audit_agent_eval", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


@pytest.mark.parametrize("name,case", module.CASES, ids=[name for name, _ in module.CASES])
def test_audit_scenario(name, case):
    scenario = module.Scenario()
    try:
        case(scenario)
    finally:
        scenario.close()
