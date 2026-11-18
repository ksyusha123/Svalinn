"""Analyze reproducible phase-based load experiments."""

import argparse
from pathlib import Path

from analyze_profiles import analyze

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    raise SystemExit(1 if analyze(args.directory) else 0)
