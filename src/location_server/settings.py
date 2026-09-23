"""サーバーの起動設定。

環境変数で与える。LAN 内利用を前提とし、書き込み系 API の共有トークンだけ任意で有効にできる
(設計文書 5 節)。構成そのもの (アンカー座標、テレメトリ送信先) は DB 側が正本なので、ここには置かない。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path("data/location.db")
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    host: str
    port: int
    auth_token: str | None


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """環境変数から設定を読む。

    - `UWB_DB_PATH`: 実験ごとに分ける SQLite ファイルのパス (設計文書 9 節)
    - `UWB_HOST` / `UWB_PORT`: uvicorn の待ち受け先
    - `UWB_AUTH_TOKEN`: 設定すると書き込み系 API に `X-Auth-Token` を要求する
    """
    source = os.environ if env is None else env
    token = source.get("UWB_AUTH_TOKEN")
    return Settings(
        db_path=Path(source.get("UWB_DB_PATH", str(DEFAULT_DB_PATH))),
        host=source.get("UWB_HOST", DEFAULT_HOST),
        port=int(source.get("UWB_PORT", str(DEFAULT_PORT))),
        auth_token=token if token else None,
    )
