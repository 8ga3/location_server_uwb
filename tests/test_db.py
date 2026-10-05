"""スキーマとマイグレーションのテスト。"""

from __future__ import annotations

import sqlite3

import pytest

from location_server import db
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


def test_migrate_from_version_1_adds_filter_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = connect(":memory:")
    try:
        # 0001 だけを当てた DB (フィルタの列を足す前) に、測位記録を 1 行書いておく
        monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS[:1])
        assert migrate(connection) == 1
        connection.execute(
            "INSERT INTO session (tag_id, boot_id, started_at, last_seen_at) VALUES (1, 1, 'a', 'a')"
        )
        connection.execute(
            """
            INSERT INTO position_fix
                (session_id, seq, t_tag_ms, recv_at, ok, x_mm, y_mm, z_mm, used_count, residual_mm, method)
            VALUES (1, 0, 1000, 'a', 1, 10, 20, 1000, 4, 42, 'trilat2d')
            """
        )
        monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
        assert migrate(connection) == MIGRATIONS[-1][0] == 3
        row = connection.execute("SELECT * FROM position_fix").fetchone()
        assert (row["ok"], row["x_mm"], row["residual_mm"]) == (1, 10, 42)
        assert (row["kf_ok"], row["kf_updated"], row["kf_init"]) == (0, 0, 0)
        for column in ("kf_x_mm", "kf_y_mm", "kf_z_mm", "kf_sigma_mm", "kf_used", "kf_rejected"):
            assert row[column] is None, column
        # 初期リビジョンは 0001 を当てたときの 1 行だけ
        assert connection.execute("SELECT count(*) FROM config_meta").fetchone()[0] == 1
    finally:
        connection.close()


def test_migrate_from_version_2_adds_range_kf_column(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = connect(":memory:")
    try:
        # 0002 までを当てた DB (測距の kf を足す前) に、測距記録を 1 行書いておく
        monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS[:2])
        assert migrate(connection) == 2
        connection.execute(
            "INSERT INTO session (tag_id, boot_id, started_at, last_seen_at) VALUES (1, 1, 'a', 'a')"
        )
        connection.execute(
            """
            INSERT INTO range_sample
                (session_id, seq, t_tag_ms, anchor_id, status, distance_mm, elapsed_ms)
            VALUES (1, 0, 1000, 256, 0, 1500, 6)
            """
        )
        monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
        assert migrate(connection) == MIGRATIONS[-1][0] == 3
        row = connection.execute("SELECT * FROM range_sample").fetchone()
        assert (row["anchor_id"], row["distance_mm"], row["elapsed_ms"]) == (256, 1500, 6)
        # 足す前の行はフィルタでの扱いを記録していないので NULL のまま
        assert row["kf"] is None
    finally:
        connection.close()
