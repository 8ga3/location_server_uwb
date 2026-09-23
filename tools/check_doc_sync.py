#!/usr/bin/env python3
"""2 つのリポジトリの `doc/` 以下が同一かを検証する。

設計メモはファームウェア側 (`m5atom_uwb_ds_twr`) とサーバー側 (`location_server_uwb`) の
両方に同じ内容で置く。片方だけを書き換える事故を防ぐため、差異があれば異常終了する。

使用例:

    python tools/check_doc_sync.py ../m5atom_uwb_ds_twr
    python tools/check_doc_sync.py ../m5atom_uwb_ds_twr --diff
"""

from __future__ import annotations

import argparse
import difflib
import filecmp
import hashlib
import sys
from pathlib import Path

# 同期対象。ここに挙げたものだけを比較する
SYNCED_PATHS: tuple[str, ...] = (
    "doc/server-design.md",
    "doc/multi-anchor-positioning-design.md",
    "doc/downlink-tdoa-design.md",
    "doc/images",
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _collect(root: Path, relative: str) -> dict[str, Path]:
    """同期対象 1 件を、リポジトリ相対パスから実パスへの辞書に展開する。"""
    target = root / relative
    if target.is_dir():
        return {str(child.relative_to(root)): child for child in sorted(target.rglob("*")) if child.is_file()}
    return {relative: target}


def compare(left: Path, right: Path) -> list[str]:
    """一致しない項目の説明を返す。すべて一致していれば空リストを返す。"""
    problems: list[str] = []
    for relative in SYNCED_PATHS:
        left_files = _collect(left, relative)
        right_files = _collect(right, relative)
        for name in sorted(set(left_files) | set(right_files)):
            left_file = left_files.get(name)
            right_file = right_files.get(name)
            if left_file is None or not left_file.exists():
                problems.append(f"{name}: {left} に存在しない")
                continue
            if right_file is None or not right_file.exists():
                problems.append(f"{name}: {right} に存在しない")
                continue
            if not filecmp.cmp(left_file, right_file, shallow=False):
                problems.append(f"{name}: 内容が異なる ({_digest(left_file)} != {_digest(right_file)})")
    return problems


def print_diff(left: Path, right: Path, relative: str) -> None:
    left_file = left / relative
    right_file = right / relative
    if not (left_file.is_file() and right_file.is_file()):
        return
    diff = difflib.unified_diff(
        left_file.read_text(encoding="utf-8").splitlines(keepends=True),
        right_file.read_text(encoding="utf-8").splitlines(keepends=True),
        fromfile=str(left_file),
        tofile=str(right_file),
    )
    sys.stdout.writelines(diff)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_doc_sync",
        description="2 つのリポジトリの doc/ 以下が同一かを検証する",
    )
    parser.add_argument("other", type=Path, help="比較するもう一方のリポジトリのパス")
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="自リポジトリのパス (既定はこのファイルの 2 階層上)",
    )
    parser.add_argument("--diff", action="store_true", help="差異のある Markdown の差分を表示する")
    args = parser.parse_args(argv)

    here = args.repo if args.repo is not None else Path(__file__).resolve().parent.parent
    other = args.other.resolve()

    if not (other / "doc").is_dir():
        print(f"比較先に doc/ がありません: {other}", file=sys.stderr)
        return 2

    problems = compare(here, other)
    if not problems:
        print(f"doc/ は一致しています: {here} と {other}")
        return 0

    print(f"doc/ に差異があります: {here} と {other}", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    if args.diff:
        for relative in SYNCED_PATHS:
            if relative.endswith(".md"):
                print_diff(here, other, relative)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
