"""受信系と保存系のテスト。欠番の追跡、不正パケットの計数、一括書き込み、UDP の結合を確認する。"""

from __future__ import annotations

import asyncio
import socket
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from location_server.api import create_app
from location_server.ingest.packet import DropReason, encode_packet
from location_server.ingest.udp import ReceiveStats, SeqTracker, TelemetryProtocol
from location_server.ingest.writer import TelemetryWriter
from location_server.settings import Settings
from location_server.store import ReceivedPacket, TelemetryStore, WriteResult
from telemetry_helpers import make_packet

ADDR = ("192.0.2.1", 50000)


# ---------------------------------------------------------------- SeqTracker


def test_tracker_counts_gaps() -> None:
    tracker = SeqTracker()
    assert tracker.observe(make_packet(seq=0, count=4)) == 0
    assert tracker.observe(make_packet(seq=4, count=4)) == 0
    # seq 8..11 が落ちた
    assert tracker.observe(make_packet(seq=12, count=4)) == 4
    state = tracker.sessions[(1, 0xAAAAAAAA)]
    assert (state.received_cycles, state.lost_cycles, state.late_cycles, state.next_seq) == (12, 4, 0, 16)
    assert tracker.total_lost_cycles == 4


def test_tracker_counts_late_packets_without_rewinding() -> None:
    tracker = SeqTracker()
    tracker.observe(make_packet(seq=0))
    tracker.observe(make_packet(seq=8))
    tracker.observe(make_packet(seq=4))
    state = tracker.sessions[(1, 0xAAAAAAAA)]
    assert (state.lost_cycles, state.late_cycles, state.next_seq) == (4, 4, 12)


def test_tracker_handles_seq_wrap() -> None:
    tracker = SeqTracker()
    tracker.observe(make_packet(seq=0xFFFFFFFE, count=2))
    assert tracker.observe(make_packet(seq=0, count=2)) == 0


def test_tracker_separates_sessions_and_forgets_on_last() -> None:
    tracker = SeqTracker()
    tracker.observe(make_packet(boot_id=1, seq=0))
    tracker.observe(make_packet(boot_id=2, seq=100))
    assert set(tracker.sessions) == {(1, 1), (1, 2)}
    tracker.observe(make_packet(boot_id=1, seq=4, flags=0x01))
    assert set(tracker.sessions) == {(1, 2)}


# ---------------------------------------------------------- TelemetryProtocol


def test_protocol_forwards_valid_packets() -> None:
    received: list[ReceivedPacket] = []
    protocol = TelemetryProtocol(received.append, SeqTracker())
    packet = make_packet()
    protocol.datagram_received(encode_packet(packet), ADDR)
    assert [item.packet for item in received] == [packet]
    assert protocol.stats.accepted_packets == 1


def test_protocol_counts_drop_reasons_and_keeps_running() -> None:
    received: list[ReceivedPacket] = []
    stats = ReceiveStats()
    protocol = TelemetryProtocol(received.append, SeqTracker(), stats)
    good = encode_packet(make_packet())
    protocol.datagram_received(b"short", ADDR)
    protocol.datagram_received(b"XXXX" + good[4:], ADDR)
    protocol.datagram_received(good[:-1], ADDR)
    protocol.datagram_received(good[:-1], ADDR)
    protocol.datagram_received(good, ADDR)
    assert stats.dropped == {
        DropReason.TOO_SHORT: 1,
        DropReason.BAD_MAGIC: 1,
        DropReason.LENGTH_MISMATCH: 2,
    }
    assert stats.dropped_packets == 4
    assert stats.accepted_packets == 1
    assert len(received) == 1


# ------------------------------------------------------------ TelemetryWriter


class _FakeStore:
    """`TelemetryStore.write_packets` の呼び出しを記録し、指定回だけ失敗させる。"""

    def __init__(self, fail: bool = False) -> None:
        self.batches: list[list[ReceivedPacket]] = []
        self.fail = fail

    def write_packets(self, received: list[ReceivedPacket]) -> WriteResult:
        if self.fail:
            raise sqlite3.OperationalError("disk I/O error")
        self.batches.append(list(received))
        rows = sum(item.packet.row_count for item in received)
        return WriteResult(packets=len(received), rows=rows, duplicate_rows=0, sessions={})


def _item(seq: int = 0, count: int = 4) -> ReceivedPacket:
    return ReceivedPacket(packet=make_packet(seq=seq, count=count), recv_at="2026-09-27T00:00:00+00:00")


def _writer(store: _FakeStore, **kwargs: float) -> TelemetryWriter:
    return TelemetryWriter(store, **kwargs)  # type: ignore[arg-type]


def test_writer_flushes_all_pending_in_one_batch() -> None:
    async def scenario() -> None:
        store = _FakeStore()
        writer = _writer(store)
        for i in range(3):
            writer.submit(_item(seq=i * 4))
        await writer.flush()
        assert len(store.batches) == 1
        assert len(store.batches[0]) == 3
        assert writer.stats.written_rows == 3 * 20
        assert writer.pending_rows == 0
        await writer.flush()
        assert len(store.batches) == 1

    asyncio.run(scenario())


def test_writer_run_flushes_on_interval_and_row_threshold() -> None:
    async def scenario() -> None:
        store = _FakeStore()
        # 間隔は十分長くし、行数の閾値で起きることを確かめる
        writer = _writer(store, flush_interval_s=10.0, flush_rows=40)
        task = asyncio.create_task(writer.run())
        writer.submit(_item(seq=0))
        await asyncio.sleep(0.05)
        assert store.batches == []
        writer.submit(_item(seq=4))
        await asyncio.sleep(0.05)
        assert len(store.batches) == 1
        task.cancel()

        timed = _FakeStore()
        writer2 = _writer(timed, flush_interval_s=0.02)
        task2 = asyncio.create_task(writer2.run())
        writer2.submit(_item())
        await asyncio.sleep(0.1)
        assert len(timed.batches) == 1
        task2.cancel()

    asyncio.run(scenario())


def test_writer_drops_oldest_when_queue_overflows() -> None:
    async def scenario() -> None:
        store = _FakeStore()
        writer = _writer(store, max_pending_rows=50)
        for i in range(4):
            writer.submit(_item(seq=i * 4))  # 1 パケット 20 行
        assert writer.stats.dropped_packets == 2
        assert writer.stats.dropped_rows == 40
        await writer.flush()
        assert [item.packet.seq for item in store.batches[0]] == [8, 12]

    asyncio.run(scenario())


def test_writer_discards_failed_batch_and_continues() -> None:
    async def scenario() -> None:
        store = _FakeStore(fail=True)
        writer = _writer(store)
        writer.submit(_item())
        await writer.flush()
        assert writer.stats.failed_batches == 1
        assert writer.stats.failed_rows == 20
        assert writer.pending_rows == 0
        store.fail = False
        writer.submit(_item(seq=4))
        await writer.flush()
        assert writer.stats.written_rows == 20

    asyncio.run(scenario())


class _SlowStore(_FakeStore):
    """書き込みに時間がかかる DB を模す。書き込みの途中かどうかを外から見られるようにする。"""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.finished = threading.Event()

    def write_packets(self, received: list[ReceivedPacket]) -> WriteResult:
        self.started.set()
        time.sleep(0.2)
        result = super().write_packets(received)
        self.finished.set()
        return result


def test_writer_stop_waits_for_in_flight_write() -> None:
    # 書き込み中に停止を求めても、DB スレッドの書き込みが終わってから run() が戻る。
    # 戻った直後に接続を閉じても、書き込みと競合しない
    async def scenario() -> None:
        store = _SlowStore()
        writer = _writer(store, flush_interval_s=0.01)
        task = asyncio.create_task(writer.run())
        writer.submit(_item(seq=0))
        await asyncio.to_thread(store.started.wait, 1.0)
        writer.submit(_item(seq=4))  # 書き込み中に届いたぶんも停止時に書き切る
        writer.request_stop()
        await task
        assert store.finished.is_set()
        assert [len(batch) for batch in store.batches] == [1, 1]

    asyncio.run(scenario())


def test_writer_with_real_store(conn: sqlite3.Connection) -> None:
    async def scenario() -> None:
        writer = TelemetryWriter(TelemetryStore(conn, threading.Lock()))
        writer.submit(_item(seq=0))
        writer.submit(_item(seq=0))  # 重複
        await writer.flush()
        assert writer.stats.written_rows == 20
        assert writer.stats.duplicate_rows == 20

    asyncio.run(scenario())


# ---------------------------------------------------------- UDP の結合テスト


def _wait_for(predicate: object, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.02)
    raise AssertionError("条件が満たされる前にタイムアウトしました")


@pytest.fixture
def udp_client(db_path: Path) -> TestClient:
    settings = Settings(db_path=db_path, host="127.0.0.1", port=0, auth_token=None, udp_port=0)
    return TestClient(create_app(settings))


def test_udp_packets_reach_database(udp_client: TestClient, db_path: Path) -> None:
    with udp_client as client:
        ingest = client.app.state.ingest  # type: ignore[attr-defined]
        port = ingest.local_port
        assert port
        hello = client.post(
            "/api/v1/hello",
            json={"tag_id": 1, "boot_id": 0xAAAAAAAA, "fw_version": "0.1.0-dev", "config_rev": 1},
        )
        assert hello.status_code == 200
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(encode_packet(make_packet(seq=0)), ("127.0.0.1", port))
            sock.sendto(b"garbage", ("127.0.0.1", port))
            sock.sendto(encode_packet(make_packet(seq=8)), ("127.0.0.1", port))
        _wait_for(lambda: ingest.writer.stats.written_rows == 40)
        assert ingest.receive_stats.dropped[DropReason.TOO_SHORT] == 1
        assert ingest.tracker.total_lost_cycles == 4

    reader = sqlite3.connect(db_path)
    try:
        sessions = reader.execute("SELECT id, fw_version FROM session").fetchall()
        assert len(sessions) == 1
        assert sessions[0][1] == "0.1.0-dev"
        assert reader.execute("SELECT count(*) FROM position_fix").fetchone()[0] == 8
        assert reader.execute("SELECT count(*) FROM range_sample").fetchone()[0] == 32
    finally:
        reader.close()


def test_pending_rows_are_flushed_on_shutdown(db_path: Path) -> None:
    settings = Settings(db_path=db_path, host="127.0.0.1", port=0, auth_token=None, udp_port=0)
    with TestClient(create_app(settings)) as client:
        ingest = client.app.state.ingest  # type: ignore[attr-defined]
        # キューへ直接積み、周期の書き込みを待たずに終了させる
        ingest.writer.submit(_item(seq=100))
    reader = sqlite3.connect(db_path)
    try:
        assert reader.execute("SELECT count(*) FROM position_fix").fetchone()[0] == 4
    finally:
        reader.close()


def test_udp_disabled_by_default(client: TestClient) -> None:
    assert client.app.state.ingest is None  # type: ignore[attr-defined]


def test_tracker_limits_number_of_sessions() -> None:
    """boot_id を変え続けられても、追跡するセッションは上限に収まる。最も長く受信していないものから忘れる。"""
    tracker = SeqTracker(max_sessions=3)
    for boot in range(1, 4):
        tracker.observe(make_packet(boot_id=boot, seq=0))
    tracker.observe(make_packet(boot_id=1, seq=4))  # boot 1 を最近受けたものにする
    tracker.observe(make_packet(boot_id=4, seq=0))
    assert set(tracker.sessions) == {(1, 1), (1, 3), (1, 4)}
    assert tracker.evicted_sessions == 1
