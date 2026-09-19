#!/usr/bin/env python3
"""Run every suite. Usage: python run_tests.py [-q]

Plain scripts rather than a test framework, so a bare checkout with numpy can
run them. `test_server.py` skips itself unless the hosted extras are present.
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SUITES = ["test_timeline.py", "test_pipeline.py", "test_workspace.py",
          "test_vault.py", "test_page.py", "test_server.py"]


def main() -> int:
    quiet = "-q" in sys.argv
    failed = []
    for name in SUITES:
        path = ROOT / "tests" / name
        r = subprocess.run([sys.executable, str(path)], capture_output=True,
                           text=True)
        lines = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()]
        tail = lines[-1] if lines else "(no output)"
        if r.returncode != 0:
            failed.append(name)
            print(f"FAIL  {name}")
            print(r.stdout[-4000:], r.stderr[-4000:], sep="\n")
        else:
            print(f"ok    {name:<20} {tail}")
            if not quiet:
                for l in lines[:-1]:
                    if l.startswith(("ok ", "skip")):
                        print(f"        {l}")
    if failed:
        print(f"\n{len(failed)} suite(s) failed: {', '.join(failed)}")
        return 1
    print("\nall suites pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
