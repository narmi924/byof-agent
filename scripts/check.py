"""Run offline development gates without rewriting baseline evidence."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    commands = [
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        [sys.executable, "-m", "mypy"],
        [sys.executable, "-m", "pytest"],
        [sys.executable, "-m", "packages.agent", "Show orders", "--mode", "mock"],
    ]
    for command in commands:
        print("Running:", " ".join(command[1:]), flush=True)
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
