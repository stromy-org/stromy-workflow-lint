"""A changed action reference and unavailable baseline must fail the public gate."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(root: str, baseline: str, report: Path):
    tools = Path(os.environ["RUNNER_TEMP"]) / "workflow-lint-tools"
    assert all((tools / name).is_file() for name in ("actionlint", "zizmor", "shellcheck"))
    result = subprocess.run(
        ["uv", "run", "--no-project", "--python", "3.12", "--with", "pyyaml==6.0.3",
         "python", str(ROOT / "lint/workflow_lint.py"), "--root", str(ROOT / root),
         "--baseline", str(ROOT / baseline), "--report", str(report)],
        cwd=ROOT, check=False, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]})
    if not report.is_file():
        raise SystemExit("native checker wrote no report: " + result.stderr[-200:])
    return result.returncode, json.loads(report.read_text())


with tempfile.TemporaryDirectory(prefix="public-lint-negative-") as scratch:
    report = Path(scratch) / "finding.json"
    code, value = check("fixtures/fail", "fixtures/fail/baseline.json", report)
    assert code == 1 and value["status"] == "FINDING" and value["counts"]["new"] >= 1
    assert any(f["rule"] == "unpinned-uses" for f in value["findings"])
    report = Path(scratch) / "unavailable.json"
    code, value = check("fixtures/pass", "fixtures/pass/missing.json", report)
    assert code == 3 and value["status"] == "NOT-RUN"
