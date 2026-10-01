"""テレメトリストアのテスト。セッションの自動生成、重複受信、トランザクション失敗を確認する。"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace

import pytest

from location_server.ingest.packet import (
    FIX_FLAG_KF_INIT,
    FIX_FLAG_KF_OK,
    FIX_FLAG_KF_UPDATED,
    FIX_FLAG_OK,
    CycleRecord,
    RangeRecord,
    TelemetryPacket,
)
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
    assert (fix["kf_ok"], fix["kf_updated"], fix["kf_init"]) == (1, 1, 0)
    assert (fix["kf_x_mm"], fix["kf_y_mm"], fix["kf_z_mm"], fix["kf_sigma_mm"]) == (1200, -5600, 1000, 35)
    assert (fix["kf_used"], fix["kf_rejected"]) == (4, 0)
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
        kf_x_mm=0,
        kf_y_mm=0,
        kf_z_mm=0,
        kf_sigma_mm=0,
        kf_used=0,
        kf_rejected=0,
        ranges=(RangeRecord(0x0100, status=11, elapsed_ms=9, distance_mm=0),),
    )
    packet = TelemetryPacket(flags=0, tag_id=2, boot_id=1, seq=0, t_tag_ms=0, anchor_n=1, cycles=(cycle,))
    telemetry.write_packets([_received(packet)])
    fix = conn.execute("SELECT * FROM position_fix").fetchone()
    assert fix["ok"] == 0
    assert (fix["x_mm"], fix["y_mm"], fix["z_mm"], fix["method"]) == (None, None, None, None)
    rng = conn.execute("SELECT * FROM range_sample").fetchone()
    assert (rng["status"], rng["distance_mm"], rng["elapsed_ms"]) == (11, None, 9)


def test_failed_fix_keeps_valid_filter(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    # 最小二乗が解けず、フィルタが予測だけで進んだサイクル。フィルタの位置は残す
    base = make_packet(count=1)
    cycle = replace(
        base.cycles[0],
        fix_flags=FIX_FLAG_KF_OK,
        x_mm=0,
        y_mm=0,
        z_mm=0,
        kf_x_mm=-2500,
        kf_y_mm=3100,
        kf_z_mm=1000,
        kf_sigma_mm=90,
        kf_used=0,
        kf_rejected=2,
    )
    telemetry.write_packets([_received(replace(base, cycles=(cycle,)))])
    fix = conn.execute("SELECT * FROM position_fix").fetchone()
    assert fix["ok"] == 0
    assert (fix["x_mm"], fix["y_mm"], fix["z_mm"], fix["method"]) == (None, None, None, None)
    assert (fix["kf_ok"], fix["kf_updated"], fix["kf_init"]) == (1, 0, 0)
    assert (fix["kf_x_mm"], fix["kf_y_mm"], fix["kf_z_mm"], fix["kf_sigma_mm"]) == (-2500, 3100, 1000, 90)
    assert (fix["kf_used"], fix["kf_rejected"]) == (0, 2)


def test_invalid_filter_stores_null(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    # フィルタが無効なら、座標欄に値が入っていても座標と標準偏差は NULL、更新と初期化のフラグは 0 にする。
    # 取り込み数と棄却数は送られた値のまま残す
    base = make_packet(count=1)
    cycle = replace(
        base.cycles[0],
        fix_flags=FIX_FLAG_OK | FIX_FLAG_KF_UPDATED | FIX_FLAG_KF_INIT,
        kf_x_mm=111,
        kf_y_mm=222,
        kf_z_mm=333,
        kf_sigma_mm=444,
        kf_used=1,
        kf_rejected=3,
    )
    telemetry.write_packets([_received(replace(base, cycles=(cycle,)))])
    fix = conn.execute("SELECT * FROM position_fix").fetchone()
    assert (fix["ok"], fix["x_mm"]) == (1, 1234)
    assert (fix["kf_ok"], fix["kf_updated"], fix["kf_init"]) == (0, 0, 0)
    assert (fix["kf_x_mm"], fix["kf_y_mm"], fix["kf_z_mm"], fix["kf_sigma_mm"]) == (None, None, None, None)
    assert (fix["kf_used"], fix["kf_rejected"]) == (1, 3)


def test_filter_init_flag(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    base = make_packet(count=1)
    flags = FIX_FLAG_OK | FIX_FLAG_KF_OK | FIX_FLAG_KF_UPDATED | FIX_FLAG_KF_INIT
    cycle = replace(base.cycles[0], fix_flags=flags)
    telemetry.write_packets([_received(replace(base, cycles=(cycle,)))])
    fix = conn.execute("SELECT kf_ok, kf_updated, kf_init FROM position_fix").fetchone()
    assert tuple(fix) == (1, 1, 1)


def test_3d_fix_method(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    base = make_packet(count=1)
    cycle = base.cycles[0]
    cycle3d = replace(cycle, fix_flags=0x03)
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
    ).session_id
    result = telemetry.write_packets([_received(make_packet())])
    assert result.sessions[(1, 0xAAAAAAAA)] == session_id
    session = conn.execute("SELECT * FROM session").fetchone()
    assert (session["fw_version"], session["config_rev"]) == ("0.1.0-dev", 7)


def test_udp_then_hello_fills_session(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    result = telemetry.write_packets([_received(make_packet(), "2026-09-27T00:00:05+00:00")])
    session_id = telemetry.hello(
        tag_id=1, boot_id=0xAAAAAAAA, fw_version="0.1.0-dev", config_rev=3, now="2026-09-27T00:00:06+00:00"
    ).session_id
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


def test_session_ids_stay_consecutive(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    # AUTOINCREMENT の表では、既存行の更新に落ちた UPSERT も番号を消費する。
    # パケットや hello を何度受けても、セッション ID は作った順の連番になる
    first = telemetry.write_packets([_received(make_packet(boot_id=1, seq=0))]).sessions[(1, 1)]
    for seq in range(4, 40, 4):
        telemetry.write_packets([_received(make_packet(boot_id=1, seq=seq))])
    telemetry.hello(
        tag_id=1, boot_id=1, fw_version="0.1.0-dev", config_rev=1, now="2026-09-27T00:00:09+00:00"
    )
    second = telemetry.hello(
        tag_id=1, boot_id=2, fw_version="0.1.0-dev", config_rev=1, now="2026-09-27T00:00:10+00:00"
    ).session_id
    third = telemetry.write_packets([_received(make_packet(boot_id=3))]).sessions[(1, 3)]
    assert (first, second, third) == (1, 2, 3)


def test_session_times_within_one_batch(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    # 1 つの束に同じセッションのパケットが順不同で入っていても、最初と最後の受信時刻を使う
    telemetry.write_packets(
        [
            _received(make_packet(seq=4), "2026-09-27T00:00:02+00:00"),
            _received(make_packet(seq=0), "2026-09-27T00:00:01+00:00"),
            _received(make_packet(seq=8), "2026-09-27T00:00:03+00:00"),
        ]
    )
    session = conn.execute("SELECT started_at, last_seen_at FROM session").fetchone()
    assert session["started_at"] == "2026-09-27T00:00:01+00:00"
    assert session["last_seen_at"] == "2026-09-27T00:00:03+00:00"


def test_duplicate_cycle_does_not_mix_ranges(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    # 同じ seq で別のアンカーを含むパケットが後から届いても、先のサイクルへ測距行を足さない
    telemetry.write_packets([_received(make_packet(count=1, anchors=(0x0100, 0x0101)))])
    result = telemetry.write_packets([_received(make_packet(count=1, anchors=(0x0102, 0x0103)))])
    assert result.rows == 0
    assert result.duplicate_rows == 3
    anchors = [row[0] for row in conn.execute("SELECT anchor_id FROM range_sample ORDER BY anchor_id")]
    assert anchors == [0x0100, 0x0101]


def test_hello_keeps_values_omitted_in_resend(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    telemetry.hello(
        tag_id=1, boot_id=1, fw_version="0.1.0-dev", config_rev=7, now="2026-09-27T00:00:00+00:00"
    )
    resent = telemetry.hello(
        tag_id=1, boot_id=1, fw_version=None, config_rev=None, now="2026-09-27T00:00:01+00:00"
    )
    # 省略した config_rev は既存の値が残り、その値を返す (ライブ配信にも同じ値を渡すため)
    assert resent.config_rev == 7
    session = conn.execute("SELECT fw_version, config_rev FROM session").fetchone()
    assert (session["fw_version"], session["config_rev"]) == ("0.1.0-dev", 7)


def test_started_at_moves_back_to_first_udp(conn: sqlite3.Connection, telemetry: TelemetryStore) -> None:
    # UDP を 00:00:01 に受けてキューへ積み、書き込む前の 00:00:02 に hello が行を作った場合
    telemetry.hello(
        tag_id=1, boot_id=0xAAAAAAAA, fw_version="0.1.0-dev", config_rev=7, now="2026-09-27T00:00:02+00:00"
    )
    telemetry.write_packets([_received(make_packet(), "2026-09-27T00:00:01+00:00")])
    session = conn.execute("SELECT started_at, last_seen_at FROM session").fetchone()
    assert session["started_at"] == "2026-09-27T00:00:01+00:00"
    assert session["last_seen_at"] == "2026-09-27T00:00:02+00:00"
