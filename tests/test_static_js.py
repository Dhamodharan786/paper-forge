"""The pages are plain HTML with inline scripts, so a typo breaks the whole screen and no Python test notices.
This checks that every inline script at least parses, using node when it is installed."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "paper_forge" / "static"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("page", sorted(p.name for p in STATIC.glob("*.html")))
def test_inline_scripts_parse(page, tmp_path):
    html = (STATIC / page).read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, f"{page} has no inline script"
    for i, code in enumerate(scripts):
        f = tmp_path / f"{page}.{i}.js"
        f.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{page} script {i} does not parse:\n{r.stderr[:600]}"
