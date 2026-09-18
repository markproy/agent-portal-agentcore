"""Runs tests/js/chart_checks.mjs, which asserts on the SVG that
static/app.js's load-test chart functions actually produce.

Shelling out to node from pytest rather than adding JS test tooling: this
repo's frontend is a single no-build-step script served straight to the
browser, and one pytest file is a smaller price than a package.json,
a runner, and a second CI job for it. Skips (rather than fails) where node
isn't installed, so the Python suite stays runnable on a bare checkout --
GitHub's ubuntu runners ship node, so CI does execute it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

CHECKS = Path(__file__).parent / "js" / "chart_checks.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_static_chart_svg_output():
    proc = subprocess.run(
        [shutil.which("node"), str(CHECKS)], capture_output=True, text=True, timeout=60
    )
    # Whole output on failure, not just the tail: each line names the
    # assertion, so the failing ones are only findable in context.
    assert proc.returncode == 0, f"\n{proc.stdout}\n{proc.stderr}"
