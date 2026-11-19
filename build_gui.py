#!/usr/bin/env python3
"""Build HumaxRW GUI packages with PyInstaller (run on the target OS).

Outputs (under dist/):
  macOS:   HumaxRW.app
  Windows: HumaxRW.exe
  Linux:   HumaxRW  (standalone binary)

PyInstaller cannot cross-compile — build the Windows .exe on Windows, etc.
For a no-freeze option on Unix, use ./humaxrw_gui.sh instead.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "humaxrw_gui.py"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
NAME = "HumaxRW"
BUNDLE_ID = "com.pauligrinder.humaxrw"


def _pyinstaller() -> list[str]:
    return [sys.executable, "-m", "PyInstaller"]


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found; installing from requirements-build.txt …")
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                str(ROOT / "requirements-build.txt"),
            ]
        )


def build_args(system: str) -> list[str]:
    common = [
        "--noconfirm",
        "--clean",
        "--name",
        NAME,
        "--paths",
        str(ROOT),
        str(ENTRY),
    ]
    if system == "Darwin":
        # onedir + windowed → HumaxRW.app (recommended; avoid onefile+.app)
        return [
            *_pyinstaller(),
            "--windowed",
            "--onedir",
            f"--osx-bundle-identifier={BUNDLE_ID}",
            *common,
        ]
    if system == "Windows":
        return [
            *_pyinstaller(),
            "--windowed",
            "--onefile",
            *common,
        ]
    # Linux and others: single binary; no .desktop chrome required
    return [
        *_pyinstaller(),
        "--onefile",
        *common,
    ]


def artifact_paths(system: str) -> list[Path]:
    if system == "Darwin":
        return [DIST / f"{NAME}.app"]
    if system == "Windows":
        return [DIST / f"{NAME}.exe"]
    return [DIST / NAME]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Do not auto-pip-install PyInstaller if missing",
    )
    args = parser.parse_args()

    if not ENTRY.is_file():
        print(f"Missing entry script: {ENTRY}", file=sys.stderr)
        return 1

    system = platform.system()
    if not args.skip_install:
        ensure_pyinstaller()

    cmd = build_args(system)
    print("Running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(ROOT))

    found = [p for p in artifact_paths(system) if p.exists()]
    if not found:
        print("Build finished but expected artifact missing under dist/", file=sys.stderr)
        return 1

    print("\nBuilt:")
    for p in found:
        size = _dir_size(p) if p.is_dir() else p.stat().st_size
        print(f"  {p}  ({_fmt(size)})")

    # Keep a convenience copy of the Unix launcher next to dist for release zips.
    launcher = ROOT / "humaxrw_gui.sh"
    if system == "Linux" and launcher.is_file():
        dest = DIST / "humaxrw_gui.sh"
        shutil.copy2(launcher, dest)
        dest.chmod(dest.stat().st_mode | 0o111)
        print(f"  {dest}  (source launcher; needs system Python + tkinter)")

    return 0


def _dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total


def _fmt(n: int) -> str:
    for unit, div in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


if __name__ == "__main__":
    raise SystemExit(main())
