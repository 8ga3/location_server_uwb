"""サーバーのバージョン。

単一の情報源は `pyproject.toml` の `[project]` セクションにある `version` である。
ここではインストール済みパッケージのメタデータから読み出すだけで、値を二重に持たない。
運用方針は AGENTS.md の「バージョン管理」を参照。
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version

DISTRIBUTION_NAME = "location-server-uwb"

# パッケージとしてインストールされていない場合 (ソースツリーを直接 import した場合など) の
# フォールバック。ファームウェア側の version.h が FW_VERSION 未定義時に "unknown" を返すのと揃える。
UNKNOWN_VERSION = "unknown"


def get_version() -> str:
    """インストール済みメタデータからバージョン文字列を返す。"""
    try:
        return _metadata_version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return UNKNOWN_VERSION


__version__ = get_version()
