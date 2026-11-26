#!/usr/bin/env python3
"""Quality ratchet: counts things we want fewer of, and fails if any count grows.

The baseline is tools/quality_baseline.json. Standard library only.

    python tools/quality_ratchet.py            # check against the baseline (CI)
    python tools/quality_ratchet.py --update   # rewrite the baseline

The rule is one-directional: a PR may lower any number, never raise it. When a
number goes down, run --update in the same PR so the gain is locked in. Running
--update to *raise* a number needs the maintainer's explicit OK in the PR.

Files over BIG_FILE_LINES may not grow, but nothing here asks anyone to split an
existing large file: put new code in a new module instead. Long functions are
reported for information only and never fail the check (function length is a
review guideline, see AGENTS.md).
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANALYZER = ROOT / "analyzer"
BASELINE = ROOT / "tools" / "quality_baseline.json"

BIG_FILE_LINES = 1500          # a file above this may not grow at all
LONG_FUNCTION_LINES = 150      # functions above this are reported (not enforced)
REPORT_ONLY = {"py_long_functions"}
TOMBSTONE = re.compile(
    r"(#|//|/\*|\*).*\b(removed|no longer|used to|was replaced|replaced by|no-op stub|Phase \d)",
    re.IGNORECASE,
)
EMPTY_JS_CATCH = re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*\}")
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _src_files(suffixes: tuple[str, ...]) -> list[Path]:
    out = []
    for p in ANALYZER.rglob("*"):
        if p.suffix in suffixes and "tests" not in p.parts and "models" not in p.parts:
            out.append(p)
    return sorted(out)


def measure() -> dict:
    m: dict = {
        "py_long_functions": 0,
        "py_except_exception_pass": 0,
        "py_dual_import_fallbacks": 0,
        "js_empty_catch": 0,
        "css_hex_outside_tokens": 0,
        "css_important": 0,
        "tombstone_comments": 0,
        "big_file_lines": {},
    }
    for p in _src_files((".py",)):
        text = p.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(text)
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if n.end_lineno - n.lineno + 1 > LONG_FUNCTION_LINES:
                    m["py_long_functions"] += 1
            if (isinstance(n, ast.ExceptHandler) and isinstance(n.type, ast.Name)
                    and n.type.id == "Exception" and len(n.body) == 1
                    and isinstance(n.body[0], ast.Pass)):
                m["py_except_exception_pass"] += 1
        m["py_dual_import_fallbacks"] += len(re.findall(r"from analyzer[. ]", text))
    for p in _src_files((".js", ".html")):
        m["js_empty_catch"] += len(EMPTY_JS_CATCH.findall(p.read_text(encoding="utf-8", errors="ignore")))
    for p in _src_files((".css",)):
        text = p.read_text(encoding="utf-8", errors="ignore")
        m["css_important"] += text.count("!important")
        if p.name != "base.css":  # base.css is where the tokens are defined
            m["css_hex_outside_tokens"] += len(HEX.findall(text))
    for p in _src_files((".py", ".js", ".css", ".html")):
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        if p.suffix != ".html":
            m["tombstone_comments"] += sum(1 for line in lines if TOMBSTONE.search(line))
        if len(lines) > BIG_FILE_LINES:
            m["big_file_lines"][str(p.relative_to(ROOT)).replace("\\", "/")] = len(lines)
    return m


def compare(base: dict, now: dict) -> list[str]:
    problems = []
    for key, value in now.items():
        if key == "big_file_lines":
            for path, n in value.items():
                limit = base["big_file_lines"].get(path)
                if limit is None:
                    problems.append(f"{path} is a new file over {BIG_FILE_LINES} lines ({n}). Split it into smaller modules.")
                elif n > limit:
                    problems.append(f"{path} grew from {limit} to {n} lines. Put new code in a new module.")
        elif key in REPORT_ONLY:
            continue
        elif value > base.get(key, 0):
            problems.append(f"{key} went up: {base.get(key, 0)} -> {value}")
    return problems


def main() -> int:
    now = measure()
    if "--update" in sys.argv or not BASELINE.exists():
        BASELINE.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(now, indent=2, sort_keys=True))
        return 0
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    problems = compare(base, now)
    for p in problems:
        print(f"RATCHET: {p}")
    print(f"py_long_functions (report only): {now['py_long_functions']} functions over {LONG_FUNCTION_LINES} lines")
    improved = [k for k, v in now.items()
                if k != "big_file_lines" and k not in REPORT_ONLY and v < base.get(k, 0)]
    if improved and not problems:
        print("Counts went down (" + ", ".join(improved) + "). Run with --update to lock it in.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
