import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from cadmcp.gateway.service import CadService  # noqa: E402


@pytest.fixture
def svc(tmp_path):
    return CadService.from_config(None, None, str(tmp_path / "ws"))
