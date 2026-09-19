"""
Properties of the served page. Run: python tests/test_page.py

The page is a Python string containing JavaScript, which is a good way to
ship a broken script: `\\n` written in a non-raw Python string becomes a real
newline, and a real newline inside a JS string literal is a syntax error. The
Python test suite cannot see that -- every server test still passes, because
the server is fine. Only the browser finds out, by rendering nothing.

So the script is parsed here. `node --check` when node is available; a
structural check either way, so a bare checkout still catches the common case.
"""

from __future__ import annotations

import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1] / "src"))

import re
import shutil
import subprocess
import tempfile

from sargam import api


def scripts(page: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", page, re.S)


def test_no_literal_newline_inside_a_js_string() -> None:
    """The failure that motivated this file. A quote opened on one line and
    never closed on it is a string literal broken by a stray newline."""
    page = api.page("/sargam")
    bad = []
    for block in scripts(page):
        for i, line in enumerate(block.split("\n"), 1):
            if "`" in line:
                continue                       # template literals may span lines
            stripped = re.sub(r"\\.", "", line)
            for q in ("'", '"'):
                if stripped.count(q) % 2 == 1:
                    bad.append((i, line.strip()[:70]))
                    break
    assert not bad, "unterminated string literal(s): " + "; ".join(
        f"line {i}: {t}" for i, t in bad)
    print("ok  no JS string literal is broken by a stray newline")


def test_the_script_parses() -> None:
    node = shutil.which("node")
    if not node:
        print("skip  node not installed; structural check only")
        return
    page = api.page("/sargam")
    for n, block in enumerate(scripts(page)):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(block)
            path = f.name
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert r.returncode == 0, f"script block {n} does not parse:\n{r.stderr}"
    print(f"ok  every script block parses ({len(scripts(page))} checked)")


def test_the_page_is_structurally_sound() -> None:
    page = api.page("/sargam")
    for tag in ("html", "head", "body", "header", "main", "style", "script"):
        o = len(re.findall(rf"<{tag}(?=[\s>])", page))
        c = len(re.findall(rf"</{tag}>", page))
        assert o == c, f"<{tag}>: {o} open, {c} close"
    assert page.lower().startswith("<!doctype html>")
    # Every element the script writes into must exist in the markup.
    for target in re.findall(r"getElementById\('([\w-]+)'\)", page):
        assert f'id="{target}"' in page, f"script writes to missing #{target}"
    print("ok  tags balance and every scripted element exists")


def test_every_handler_the_page_calls_is_routed() -> None:
    """An onclick naming an endpoint the server does not serve is a button
    that silently does nothing."""
    page = api.page("/sargam")
    called = set(re.findall(r"post(?:Status)?\('(/api/[\w/]+)'", page))
    served = set(api.ROUTES) | {"/api/key", "/api/key/clear",
                                "/api/account/delete"}
    missing = called - served
    assert not missing, f"page calls unrouted endpoint(s): {sorted(missing)}"
    print(f"ok  every endpoint the page posts to is routed ({len(called)} checked)")


if __name__ == "__main__":
    test_no_literal_newline_inside_a_js_string()
    test_the_script_parses()
    test_the_page_is_structurally_sound()
    test_every_handler_the_page_calls_is_routed()
    print("\nall page properties hold")
