"""SQLite の接続とマイグレーション。

単一プロセス・単一ライターで足りるため、WAL モードの接続を 1 本だけ持つ (設計文書 3 節)。
異常終了時に直近のコミット未満を失うことは許容する方針に従い、`synchronous` は `NORMAL` とする。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

# (user_version, SQL ファイル名) を昇順に並べる
MIGRATIONS: tuple[tuple[int, str], ...] = ((1, "0001_initial.sql"),)

INITIAL_PAN_ID = 0xDECA
INITIAL_BATCH_CYCLES = 4


def utc_now_text() -> str:
    """DB とレスポンスで共通に使う時刻表記。"""
    return datetime.now(UTC).isoformat()


def connect(db_path: Path | str) -> sqlite3.Connection:
    """WAL モードの接続を開く。`:memory:` を渡すとテスト用のインメモリ DB になる。"""
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None で暗黙のトランザクションを止め、開始と終了を明示的に書く
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _load_sql(name: str) -> str:
    return (resources.files("location_server.migrations") / name).read_text(encoding="utf-8")


def _split_statements(script: str) -> list[str]:
    """SQL スクリプトを 1 文ずつに分割する。

    `Connection.executescript()` は実行前に暗黙の COMMIT を出すため、
    マイグレーション全体を 1 トランザクションに収めるには使えない。
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer)
            buffer = ""
    if buffer.strip():
        raise ValueError("SQL スクリプトの末尾が文として閉じていません")
    return statements


def _seed_initial_config(conn: sqlite3.Connection) -> None:
    """初期リビジョンを 1 行だけ作る。

    アンカーは未登録、`telemetry_port` は 0 (タグ側のテレメトリ送信停止) から始める。
    """
    conn.execute(
        """
        INSERT INTO config_meta
            (rev, pan_id, bias_mm, telemetry_host, telemetry_port, batch_cycles, note, created_at)
        VALUES (1, ?, 0, '', 0, ?, '初期リビジョン', ?)
        """,
        (INITIAL_PAN_ID, INITIAL_BATCH_CYCLES, utc_now_text()),
    )


def migrate(conn: sqlite3.Connection) -> int:
    """未適用のマイグレーションを順に適用し、適用後のスキーマ版を返す。"""
    current: int = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, filename in MIGRATIONS:
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_statements(_load_sql(filename)):
                conn.execute(statement)
            if version == 1:
                _seed_initial_config(conn)
            # PRAGMA はパラメータを取れないため、定数を直接埋め込む
            conn.execute(f"PRAGMA user_version = {version:d}")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        current = version
    return current
