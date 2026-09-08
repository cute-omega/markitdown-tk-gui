#!/usr/bin/env python3
"""一键发版脚本：更新 pyproject.toml 版本号、提交、打 tag。"""

import subprocess
import sys
import re
from pathlib import Path

PYPROJECT = Path(__file__).parent / "pyproject.toml"


def get_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"(.+)"', text, re.MULTILINE)
    if not m:
        raise RuntimeError("无法从 pyproject.toml 读取版本号")
    return m.group(1)


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("major", "minor", "patch"):
        print("用法: python release.py <major|minor|patch>")
        print("示例: python release.py minor")
        sys.exit(1)

    bump = sys.argv[1]

    run(["uv", "version", "--bump", bump])
    version = get_version()
    print(f"版本已更新到 {version}")

    run(["git", "add", "pyproject.toml"])
    run(["git", "commit", "-m", f"chore: release v{version}"])

    print()
    print(f"已提交版本 {version}，执行以下命令推送：")
    print(f"  git push origin main")


if __name__ == "__main__":
    main()
