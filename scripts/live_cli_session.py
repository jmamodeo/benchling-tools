"""Interactive CLI smoke tests.

Log in first, in your own terminal, so the client secret never enters this
script or an agent. State for the session is isolated below:

    BENCHLING_TOOLS_HOME=work/live-test/state benchling-tools auth login --env dev

Then send one JSON object per line: {"args": ["entry", "inspect", "etr_...", "--env", "dev"]}.
Optional `artifact` saves the generated CLI output under ignored work/live-test.
Optional `select` limits displayed data keys, leaving the artifact unchanged.
The session invokes the real CLI for every operation, including plan/apply.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    """Relay JSON command lines from stdin to the real CLI, printing one JSON result per line."""
    root = Path(__file__).resolve().parents[1]
    output_dir = root / "work" / "live-test"
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = dict(os.environ)
    env["BENCHLING_TOOLS_HOME"] = str(output_dir / "state")
    executable = root / ".venv" / "bin" / "benchling-tools"
    print(
        "CLI test session ready. Environments come from "
        f"{env['BENCHLING_TOOLS_HOME']}. Send JSON command objects; exit with EOF.",
        flush=True,
    )
    for line in sys.stdin:
        request = json.loads(line)
        args = request["args"]
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("args must be a list of strings")
        result = subprocess.run(
            [str(executable), *args], env=env, cwd=root, text=True, capture_output=True
        )
        payload = json.loads(result.stdout) if result.stdout.strip() else None
        artifact = request.get("artifact")
        if artifact:
            if Path(artifact).name != artifact:
                raise ValueError("artifact must be a filename")
            target = output_dir / artifact
            target.write_text(result.stdout, encoding="utf-8")
            target.chmod(0o600)
        selected = request.get("select")
        if selected and payload and isinstance(payload.get("data"), dict):
            payload["data"] = {key: payload["data"].get(key) for key in selected}
        print(
            json.dumps(
                {
                    "args": args,
                    "exit_code": result.returncode,
                    "output": payload,
                    "stderr": result.stderr,
                    "artifact": artifact,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
