#!/usr/bin/env python3
"""Verify YAML in every deep-dive parses correctly."""
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML not installed — install with: pip install pyyaml")
    sys.exit(1)

ROOT = Path(__file__).parent
files = sorted(ROOT.glob("0*-*.md")) + [ROOT / "README.md"]

total_blocks = 0
total_fail = 0

for f in files:
    text = f.read_text()
    # Extract fenced code blocks EXPLICITLY tagged as yaml (skip untagged ones —
    # they may be shell, JSON, or bash heredocs that look YAML-ish).
    blocks = re.findall(r"```(?:yaml|YAML)\n(.*?)```", text, re.DOTALL)
    n_ok = 0
    n_fail = 0
    for block in blocks:
        total_blocks += 1
        try:
            # Use safe_load_all to handle multi-document YAML
            list(yaml.safe_load_all(block))
            n_ok += 1
        except yaml.YAMLError as e:
            n_fail += 1
            total_fail += 1
            print(f"  FAIL in {f.name}: {str(e)[:120]}")
    print(f"{f.name}: {n_ok} OK, {n_fail} FAIL ({len(blocks)} total blocks)")

print(f"\n=== {total_blocks - total_fail}/{total_blocks} YAML blocks parse cleanly ===")
sys.exit(0 if total_fail == 0 else 1)
