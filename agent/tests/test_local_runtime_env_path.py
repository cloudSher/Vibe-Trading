"""A local runtime override must isolate both settings writes and LLM reads."""

import json
import os
from pathlib import Path
import subprocess
import sys


def test_runtime_override_is_used_by_settings_cli_and_model(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".env").write_text("LOCAL_RUNTIME_PROBE=isolated\n")
    env = os.environ.copy()
    env["VIBE_TRADING_HOME"] = str(runtime)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env.pop("LOCAL_RUNTIME_PROBE", None)
    result = subprocess.run(
        [sys.executable, "-c", """
import json, os
from src.api import helpers
from src.providers import llm
from cli import _legacy
llm._ensure_dotenv()
print(json.dumps({
    'settings': str(helpers.ENV_PATH),
    'cli': str(_legacy._INIT_ENV_PATH),
    'model': str(llm._ENV_CANDIDATES[0]),
    'probe': os.environ.get('LOCAL_RUNTIME_PROBE'),
}))
"""],
        env=env, cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    assert paths == {
        "settings": str(runtime / ".env"),
        "cli": str(runtime / ".env"),
        "model": str(runtime / ".env"),
        "probe": "isolated",
    }
