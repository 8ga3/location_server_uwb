"""テレメトリストアのテスト。セッションの自動生成、重複受信、トランザクション失敗を確認する。"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from location_server.ingest.packet import CycleRecord, RangeRecord, TelemetryPacket
from location_server.store import ReceivedPacket, TelemetryStore
from telemetry_helpers import make_packet


@pytest.fixture
def telemetry(conn: sqlite3.Connection) -> TelemetryStore:
    return TelemetryStore(conn, threading.Lock())


def _received(packet: TelemetryPacket, recv_at: str = "2026-09-27T00:00:00+00:00") -> ReceivedPacket:
    return ReceivedPacket(packet=packet, recv_at=recv_at)


def _count(conn: sqlite3.Connection, table: str) -> int:
    count: int = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return count


def test_write_creates_session_and_rows(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    result = telemetry.write_packets([_received(make_packet(count=4))])
    assert result.rows == 4 * (1 + 4)
    assert result.duplicate_rows == 0
    session = conn.execute("SELECT * FROM session").fetchone()
    assert (session["tag_id"], session["boot_id"]) == (1, 0xAAAAAAAA)
    # UDP で自動生成したセッションは fw_version と config_rev を持たない
    assert session["fw_version"] is None
    assert session["config_rev"] is None
    assert result.sessions == {(1, 0xAAAAAAAA): session["id"]}
    assert _count(conn, "position_fix") == 4
    assert _count(conn, "range_sample") == 16


def test_rows_keep_values(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    telemetry.write_packets([_received(make_packet(seq=7, t_tag_ms=2000, count=1))])
    fix = conn.execute("SELECT * FROM position_fix").fetchone()
    assert (fix["seq"], fix["t_tag_ms"], fix["ok"]) == (7, 2000, 1)
    assert (fix["x_mm"], fix["y_mm"], fix["z_mm"]) == (1234, -5678, 1000)
    assert (fix["used_count"], fix["residual_mm"], fix["method"]) == (4, 42, "trilat2d")
    rng = conn.execute("SELECT * FROM range_sample WHERE anchor_id = 0x0101").fetchone()
    assert (rng["seq"], rng["status"], rng["distance_mm"], rng["elapsed_ms"]) == (7, 0, 1100, 6)


def test_failed_fix_and_range_store_null(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    cycle = CycleRecord(
        seq=0,
        t_tag_ms=0,
        fix_flags=0,
        used_count=1,
        x_mm=0,
        y_mm=0,
        z_mm=0,
        residual_mm=0,
        ranges=(RangeRecord(0x0100, status=11, elapsed_ms=9, distance_mm=0),),
    )
    packet = TelemetryPacket(flags=0, tag_id=2, boot_id=1, seq=0, t_tag_ms=0, anchor_n=1, cycles=(cycle,))
    telemetry.write_packets([_received(packet)])
    fix = conn.execute("SELECT * FROM position_fix").fetchone()
    assert fix["ok"] == 0
    assert (fix["x_mm"], fix["y_mm"], fix["z_mm"], fix["method"]) == (None, None, None, None)
    rng = conn.execute("SELECT * FROM range_sample").fetchone()
    assert (rng["status"], rng["distance_mm"], rng["elapsed_ms"]) == (11, None, 9)


def test_3d_fix_method(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    base = make_packet(count=1)
    cycle = base.cycles[0]
    cycle3d = CycleRecord(
        seq=cycle.seq,
        t_tag_ms=cycle.t_tag_ms,
        fix_flags=0x03,
        used_count=cycle.used_count,
        x_mm=cycle.x_mm,
        y_mm=cycle.y_mm,
        z_mm=cycle.z_mm,
        residual_mm=cycle.residual_mm,
        ranges=cycle.ranges,
    )
    packet = TelemetryPacket(
        flags=0, tag_id=1, boot_id=1, seq=cycle.seq, t_tag_ms=cycle.t_tag_ms, anchor_n=4, cycles=(cycle3d,)
    )
    telemetry.write_packets([_received(packet)])
    assert conn.execute("SELECT method FROM position_fix").fetchone()[0] == "trilat3d"


def test_duplicate_packets_are_ignored(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    packet = make_packet(count=2)
    telemetry.write_packets([_received(packet)])
    result = telemetry.write_packets([_received(packet), _received(packet)])
    assert result.rows == 0
    assert result.duplicate_rows == 2 * 2 * 5
    assert _count(conn, "session") == 1
    assert _count(conn, "position_fix") == 2


def test_sessions_are_separated_by_boot_id(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    telemetry.write_packets(
        [
            _received(make_packet(boot_id=1)),
            _received(make_packet(boot_id=2)),
            _received(make_packet(tag_id=2)),
        ]
    )
    assert _count(conn, "session") == 3


def test_last_seen_advances_but_started_at_stays(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    telemetry.write_packets([_received(make_packet(seq=0), "2026-09-27T00:00:01+00:00")])
    telemetry.write_packets([_received(make_packet(seq=4), "2026-09-27T00:00:03+00:00")])
    # 順序が入れ替わって古い時刻のパケットが後から届いても last_seen_at は戻らない
    telemetry.write_packets([_received(make_packet(seq=8), "2026-09-27T00:00:02+00:00")])
    session = conn.execute("SELECT started_at, last_seen_at FROM session").fetchone()
    assert session["started_at"] == "2026-09-27T00:00:01+00:00"
    assert session["last_seen_at"] == "2026-09-27T00:00:03+00:00"


def test_hello_then_udp_share_session(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    session_id = telemetry.hello(
        tag_id=1, boot_id=0xAAAAAAAA, fw_version="0.1.0-dev", config_rev=7, now="2026-09-27T00:00:00+00:00"
    )
    result = telemetry.write_packets([_received(make_packet())])
    assert result.sessions[(1, 0xAAAAAAAA)] == session_id
    session = conn.execute("SELECT * FROM session").fetchone()
    assert (session["fw_version"], session["config_rev"]) == ("0.1.0-dev", 7)


def test_udp_then_hello_fills_session(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    result = telemetry.write_packets([_received(make_packet(), "2026-09-27T00:00:05+00:00")])
    session_id = telemetry.hello(
        tag_id=1, boot_id=0xAAAAAAAA, fw_version="0.1.0-dev", config_rev=3, now="2026-09-27T00:00:06+00:00"
    )
    assert session_id == result.sessions[(1, 0xAAAAAAAA)]
    session = conn.execute("SELECT * FROM session").fetchone()
    assert (session["fw_version"], session["config_rev"]) == ("0.1.0-dev", 3)
    assert session["started_at"] == "2026-09-27T00:00:05+00:00"
    assert _count(conn, "session") == 1


def test_failed_transaction_rolls_back(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    telemetry.write_packets([_received(make_packet(boot_id=1))])
    # 測距の表を壊して、束の途中で書き込みを失敗させる
    conn.execute("DROP TABLE range_sample")
    with pytest.raises(sqlite3.OperationalError):
        telemetry.write_packets([_received(make_packet(boot_id=2))])
    # 同じ束で作ったセッションと測位の行も残らない
    assert _count(conn, "session") == 1
    assert _count(conn, "position_fix") == 4
    assert not conn.in_transaction
