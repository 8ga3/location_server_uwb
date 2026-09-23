"""スキーマとマイグレーションのテスト。"""

from __future__ import annotations

import sqlite3

from location_server.db import MIGRATIONS, connect, migrate

EXPECTED_TABLES = {
    "anchor",
    "config_meta",
    "config_anchor",
    "session",
    "range_sample",
    "position_fix",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def test_migrate_creates_all_tables(conn: sqlite3.Connection) -> None:
    assert _table_names(conn) >= EXPECTED_TABLES


def test_migrate_sets_user_version(conn: sqlite3.Connection) -> None:
    expected = MIGRATIONS[-1][0]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == expected


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    before = conn.execute("SELECT count(*) FROM config_meta").fetchone()[0]
    migrate(conn)
    migrate(conn)
    after = conn.execute("SELECT count(*) FROM config_meta").fetchone()[0]
    assert before == after == 1


def test_initial_revision_matches_design_defaults(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT * FROM config_meta WHERE rev = 1").fetchone()
    assert row["pan_id"] == 0xDECA
    assert row["bias_mm"] == 0
    # port = 0 はタグ側のテレメトリ送信停止を意味する
    assert row["telemetry_port"] == 0
    assert row["batch_cycles"] == 4


def test_foreign_keys_are_enforced(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            "INSERT INTO config_anchor (rev, id, label, x_mm, y_mm, z_mm, enabled, source) "
            "VALUES (999, 256, NULL, 0, 0, 0, 1, 'manual')"
        )
    except sqlite3.IntegrityError:
        return
    raise AssertionError("存在しない rev への参照が拒否されませんでした")


def test_wal_mode_on_file_database(db_path: object) -> None:
    connection = connect(str(db_path))
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        connection.close()
