"""The JSON boundary must stay UTF-8 outside UTF-8-configured test runners."""

import json
import os
from pathlib import Path
import subprocess
import sys


def test_redirected_json_preserves_chinese_under_legacy_encoding() -> None:
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="cp1252")
    result = subprocess.run(
        [sys.executable, "-m", "bbag", "validate", "--config", "configs/s0.example.json"],
        cwd=root, env=environment, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8")
    output = json.loads(result.stdout.decode("utf-8"))
    assert output["valid"] is True
    assert "正式市场范围" in output["pending_decisions"][0]
