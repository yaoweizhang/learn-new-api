"""Meta-test: every chapter README satisfies a small set of structural
contracts. Catches docs rot (missing nav, no runnable command, dropped
new-api mapping) without policing prose style.
"""

import py_compile
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHAPTERS = sorted(p for p in ROOT.glob("s[0-9][0-9]_*") if p.is_dir())
S_FULL = ROOT / "s_full"
ALL = CHAPTERS + [S_FULL]


def test_chapter_dirs_complete():
    """All sNN_* + s_full directories must exist."""
    missing = {p.name for p in ALL} - {p.name for p in ROOT.iterdir() if p.is_dir()}
    assert not missing, f"Missing chapter dirs: {sorted(missing)}"


@pytest.mark.parametrize("chap", ALL, ids=lambda p: p.name)
def test_readme_and_code_exist(chap):
    assert (chap / "README.md").exists(), f"{chap.name}/README.md missing"
    assert (chap / "code.py").exists(), f"{chap.name}/code.py missing"


@pytest.mark.parametrize("chap", ALL, ids=lambda p: p.name)
def test_code_compiles(chap):
    py_compile.compile(str(chap / "code.py"), doraise=True)


@pytest.mark.parametrize("chap", ALL, ids=lambda p: p.name)
def test_readme_has_h1(chap):
    text = (chap / "README.md").read_text(encoding="utf-8")
    first = next((l for l in text.splitlines() if l.strip()), "")
    assert first.startswith("# "), f"{chap.name}: first line not H1 ({first!r})"


@pytest.mark.parametrize("chap", ALL, ids=lambda p: p.name)
def test_readme_has_previous_next_nav(chap):
    text = (chap / "README.md").read_text(encoding="utf-8")
    assert re.search(r"Previous:|Next:", text), (
        f"{chap.name}: missing Previous/Next nav link"
    )


@pytest.mark.parametrize("chap", ALL, ids=lambda p: p.name)
def test_readme_has_bash_block(chap):
    text = (chap / "README.md").read_text(encoding="utf-8")
    assert re.search(r"```(bash|sh)\b", text), (
        f"{chap.name}: missing runnable shell code block (```bash or ```sh)"
    )


@pytest.mark.parametrize("chap", ALL, ids=lambda p: p.name)
def test_readme_has_new_api_mapping(chap):
    text = (chap / "README.md").read_text(encoding="utf-8")
    assert "new-api" in text and "源码" in text, (
        f"{chap.name}: missing '→ new-api 源码' section"
    )