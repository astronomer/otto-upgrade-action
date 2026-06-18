"""Make the action's scripts importable as modules for unit tests."""

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())
