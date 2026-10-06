"""Make the source tree importable without installing the application."""
from pathlib import Path
import sys
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture
def core_tmp_path():
    """Keep core-test files within the workspace in restricted Windows runs."""
    with tempfile.TemporaryDirectory(prefix=".core-test-", dir=Path(__file__).parent) as directory:
        yield Path(directory)
