"""ライブ配信のハブのテスト。snapshot / append、セッションの開始と終了、遅延と重複、キューの溢れ。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from location_server.ingest.packet import FIX_FLAG_KF_OK, RangeRecord, TelemetryPacket
from location_server.live import LiveHub, Subscriber, SubscriberOverflowError
from location_server.live.hub import END_NEW_SESSION, END_TAG_LAST, END_TIMEOUT
from telemetry_helpers import make_packet


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _hub(**kwargs: Any) -> tuple[LiveHub, FakeClock]:
    clock = FakeClock()
    return LiveHub(clock=clock, **kwargs), clock


def _drain(subscriber: Subscriber) -> list[dict[str, Any]]:
    frames = []
    while (frame := subscriber.pop_nowait()) is not None:
        frames.append(frame)
    return frames


def _types(frames: list[dict[str, Any]]) -> list[str]:
    return [frame["type"] for frame in frames]


def test_subscribe_without_session_returns_empty_snapshot() -> None:
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1, 30000)
    [snapshot] = _drain(sub)
    assert snapshot["type"] == "snapshot"
    assert snapshot["session_id"] is None
    assert snapshot["active"] is False
    assert snapshot["fix"]["t"] == []
    assert snapshot["ranges"] == {}


def test_publish_then_tick_sends_session_start_and_append() -> None:
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    _drain(sub)
    hub.publish(make_packet(seq=0, t_tag_ms=1000, count=4, period_ms=50))
    frames = _drain(sub)
    assert _types(frames) == ["session_start"]
    assert frames[0]["boot_id"] == 0xAAAAAAAA
    assert frames[0]["session_id"] is None

    hub.tick()
    [append] = _drain(sub)
    assert append["type"] == "append"
    assert append["fix"]["t"] == [1000, 1050, 1100, 1150]
    assert append["fix"]["seq"] == [0, 1, 2, 3]
    assert append["fix"]["dt"] == [None, 50, 50, 50]
    assert append["fix"]["x"] == [1.234] * 4
    assert append["fix"]["y"] == [-5.678] * 4
    assert append["fix"]["ok"] == [True] * 4
    # フィルタの列も参照 API と同じ列名・単位 (メートル) で載る
    assert append["fix"]["kx"] == [1.2] * 4
    assert append["fix"]["ky"] == [-5.6] * 4
    assert append["fix"]["kz"] == [1.0] * 4
    assert append["fix"]["ksig"] == [0.035] * 4
    assert append["fix"]["kok"] == [True] * 4
    assert append["fix"]["kupd"] == [True] * 4
    assert append["fix"]["kinit"] == [False] * 4
    assert append["fix"]["kused"] == [4] * 4
    assert append["fix"]["krej"] == [0] * 4
    assert set(append["ranges"]) == {"0x0100", "0x0101", "0x0102", "0x0103"}
    assert append["ranges"]["0x0101"]["d"] == [1.1] * 4
    # 次の tick までに新しいサイクルが無ければ何も送らない
    hub.tick()
    assert _drain(sub) == []


def test_snapshot_excludes_pending_cycles_and_respects_history() -> None:
    hub, _ = _hub()
    hub.publish(make_packet(seq=0, t_tag_ms=0, count=10, period_ms=1000))
    hub.tick()
    # まだ差分として送っていないサイクルは snapshot に入れず、次の append で送る
    hub.publish(make_packet(seq=10, t_tag_ms=10_000, count=1))
    sub = hub.connect()
    hub.subscribe(sub, 1, history_ms=3000)
    [snapshot] = _drain(sub)
    assert snapshot["fix"]["seq"] == [6, 7, 8, 9]
    assert snapshot["active"] is True
    hub.tick()
    [append] = _drain(sub)
    assert append["fix"]["seq"] == [10]


def test_other_tags_are_not_delivered() -> None:
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 2)
    _drain(sub)
    hub.publish(make_packet(tag_id=1))
    hub.tick()
    assert _drain(sub) == []


def test_late_and_duplicate_cycles_are_dropped() -> None:
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    hub.publish(make_packet(seq=8, count=4))
    hub.publish(make_packet(seq=4, count=4))  # 遅れて届いた
    hub.publish(make_packet(seq=8, count=4))  # 重複
    hub.publish(make_packet(seq=10, t_tag_ms=1100, count=4))  # 前半 2 サイクルが重複
    hub.tick()
    frames = _drain(sub)
    append = frames[-1]
    assert append["fix"]["seq"] == [8, 9, 10, 11, 12, 13]
    # パケットをまたいでも隣の seq なら周期を求める
    assert append["fix"]["dt"] == [None, 50, 50, 50, 50, 50]
    assert hub.stats.late_cycles == 10


def test_timeout_ends_session_and_same_boot_resumes() -> None:
    hub, clock = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    _drain(sub)
    hub.publish(make_packet(seq=0))
    hub.tick()
    _drain(sub)
    clock.now += 2.9
    hub.tick()
    assert _drain(sub) == []
    clock.now += 0.2
    hub.tick()
    [end] = _drain(sub)
    assert end == {
        "type": "session_end",
        "session_id": None,
        "tag_id": 1,
        "boot_id": 0xAAAAAAAA,
        "reason": END_TIMEOUT,
    }
    # 終了後も最後の状態は snapshot で見られる
    assert hub.snapshot(1)["active"] is False
    assert len(hub.snapshot(1)["fix"]["t"]) == 4

    hub.publish(make_packet(seq=4))
    hub.tick()
    frames = _drain(sub)
    assert _types(frames) == ["session_start", "append"]
    assert frames[0]["boot_id"] == 0xAAAAAAAA
    assert hub.sessions[1].active


def test_new_boot_ends_previous_session_and_ignores_stale_packets() -> None:
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    _drain(sub)
    hub.publish(make_packet(boot_id=1, seq=0))
    hub.publish(make_packet(boot_id=2, seq=0))
    frames = _drain(sub)
    # 旧セッションの未送信ぶんを送り切ってから終了を通知する
    assert _types(frames) == ["session_start", "append", "session_end", "session_start"]
    assert frames[1]["boot_id"] == 1
    assert frames[2]["reason"] == END_NEW_SESSION
    assert frames[3]["boot_id"] == 2

    hub.publish(make_packet(boot_id=1, seq=4))
    hub.tick()
    frames = _drain(sub)
    assert _types(frames) == ["append"]
    assert frames[0]["boot_id"] == 2
    assert hub.stats.stale_cycles == 4


def test_last_flag_ends_session() -> None:
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    _drain(sub)
    hub.publish(make_packet(seq=0, flags=0x01))
    frames = _drain(sub)
    assert _types(frames) == ["session_start", "append", "session_end"]
    assert frames[2]["reason"] == END_TAG_LAST
    # 同じ起動のパケットが後から来ても再開しない
    hub.publish(make_packet(seq=4))
    hub.tick()
    assert _drain(sub) == []


def test_ring_buffer_keeps_retention_window_and_count_limit() -> None:
    hub, _ = _hub(retention_ms=1000, max_buffer_cycles=8)
    hub.publish(make_packet(seq=0, t_tag_ms=0, count=4, period_ms=100))
    hub.publish(make_packet(seq=4, t_tag_ms=1500, count=4, period_ms=100))
    hub.tick()
    assert [c.seq for c in hub.sessions[1].cycles] == [4, 5, 6, 7]
    hub.publish(make_packet(seq=8, t_tag_ms=1900, count=16, period_ms=1))
    assert len(hub.sessions[1].cycles) == 8
    assert hub.stats.overflow_cycles == 12


def test_slow_subscriber_drops_oldest_appends_but_keeps_control_frames() -> None:
    hub, clock = _hub(subscriber_max_frames=3)
    sub = hub.connect()
    hub.subscribe(sub, 1)
    for i in range(6):
        hub.publish(make_packet(seq=i * 4))
        hub.tick()
    clock.now += 5
    hub.tick()
    frames = _drain(sub)
    # snapshot と session_start / session_end は残り、append は新しいものだけが残る
    assert _types(frames) == ["snapshot", "session_start", "session_end"]
    assert sub.lost == 6
    assert hub.stats.dropped_frames == 6

    hub.subscribe(sub, 1)
    assert sub.lost == 0


def test_subscriber_queue_keeps_newest_appends() -> None:
    hub, _ = _hub(subscriber_max_frames=4)
    sub = hub.connect()
    hub.subscribe(sub, 1)
    _drain(sub)
    hub.publish(make_packet(seq=0))
    _drain(sub)  # session_start
    for i in range(1, 7):
        hub.publish(make_packet(seq=i * 4))
        hub.tick()
    frames = _drain(sub)
    assert [f["fix"]["seq"][0] for f in frames] == [12, 16, 20, 24]
    assert sub.lost == 2


def test_hello_before_udp_fills_session_id_and_config_rev() -> None:
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    _drain(sub)
    hub.note_hello(1, 0xAAAAAAAA, 12, 7)
    hub.publish(make_packet(seq=0))
    [start] = _drain(sub)
    assert (start["session_id"], start["config_rev"]) == (12, 7)


def test_hello_after_udp_resends_session_start() -> None:
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    hub.publish(make_packet(seq=0))
    _drain(sub)
    hub.note_hello(1, 0xAAAAAAAA, 12, 7)
    [start] = _drain(sub)
    assert start["type"] == "session_start"
    assert (start["session_id"], start["config_rev"]) == (12, 7)
    # 構成リビジョンが変わらない hello では送り直さない
    hub.note_hello(1, 0xAAAAAAAA, 12, 7)
    assert _drain(sub) == []


def test_committed_session_id_is_attached_to_later_appends() -> None:
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    hub.publish(make_packet(seq=0))
    hub.note_sessions({(1, 0xAAAAAAAA): 5, (9, 1): 6})
    hub.publish(make_packet(seq=4))
    hub.tick()
    assert _drain(sub)[-1]["session_id"] == 5


def test_failed_fix_and_failed_range_are_null() -> None:
    hub, _ = _hub()
    packet = make_packet(seq=0, count=1)
    cycle = packet.cycles[0]
    failed = TelemetryPacket(
        flags=0,
        tag_id=1,
        boot_id=packet.boot_id,
        seq=0,
        t_tag_ms=cycle.t_tag_ms,
        anchor_n=packet.anchor_n,
        cycles=(
            type(cycle)(
                seq=0,
                t_tag_ms=cycle.t_tag_ms,
                fix_flags=0,
                used_count=2,
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
                ranges=(RangeRecord(0x0100, 3, 7, 0), *cycle.ranges[1:]),
            ),
        ),
    )
    hub.publish(failed)
    snapshot_after = hub.connect()
    hub.tick()
    hub.subscribe(snapshot_after, 1)
    [snapshot] = _drain(snapshot_after)
    assert snapshot["fix"]["ok"] == [False]
    assert snapshot["fix"]["x"] == [None]
    assert snapshot["fix"]["used"] == [2]
    assert snapshot["fix"]["kok"] == [False]
    assert snapshot["fix"]["kx"] == [None]
    assert snapshot["fix"]["ksig"] == [None]
    assert (snapshot["fix"]["kused"], snapshot["fix"]["krej"]) == ([0], [0])
    assert snapshot["ranges"]["0x0100"] == {"t": [1000], "seq": [0], "d": [None], "st": [3], "el": [7]}


def test_millis_and_seq_wrap_are_unrolled() -> None:
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    hub.publish(make_packet(seq=0xFFFFFFFE, t_tag_ms=0xFFFFFFFF - 150, count=4, period_ms=100))
    hub.tick()
    append = _drain(sub)[-1]
    start = 0xFFFFFFFF - 150
    assert append["fix"]["seq"] == [0xFFFFFFFE, 0xFFFFFFFF, 2**32, 2**32 + 1]
    assert append["fix"]["t"] == [start, start + 100, start + 200, start + 300]
    assert append["fix"]["dt"] == [None, 100, 100, 100]
    assert append["ranges"]["0x0100"]["t"] == append["fix"]["t"]
    # 折り返した後のパケットも遅延扱いにならず、時刻が続く
    hub.publish(make_packet(seq=2, t_tag_ms=start + 400 - 2**32, count=1))
    hub.tick()
    append = _drain(sub)[-1]
    assert (append["fix"]["seq"], append["fix"]["t"], append["fix"]["dt"]) == (
        [2**32 + 2],
        [start + 400],
        [100],
    )


def test_invalid_requests_do_not_accumulate_errors() -> None:
    hub, _ = _hub(subscriber_max_frames=3)
    sub = hub.connect()
    for i in range(100):
        sub.push({"type": "error", "detail": str(i)})
    frames = _drain(sub)
    # 未送信の error は最新の 1 つだけ残す
    assert frames == [{"type": "error", "detail": "99"}]
    assert not sub.overflowed


def test_control_frame_flood_overflows_and_disconnects() -> None:
    hub, _ = _hub(subscriber_max_frames=3)
    sub = hub.connect()
    hub.subscribe(sub, 1)
    # boot_id を変え続けると session_end / session_start が積み上がる
    for boot in range(1, 10):
        hub.publish(make_packet(boot_id=boot, seq=0, count=1))
    assert sub.overflowed
    assert sub.pending_frames == 0
    assert hub.stats.overflow_disconnects == 1

    async def wait() -> None:
        await sub.next()

    with pytest.raises(SubscriberOverflowError):
        asyncio.run(wait())


def test_stats_log_records_active_session_change(caplog: pytest.LogCaptureFixture) -> None:
    """timeout で稼働中のセッションが 0 になっただけでも LIVE_STATS を出し直す。"""
    hub, clock = _hub(stats_interval_s=0.01)
    hub.publish(make_packet(seq=0))

    async def scenario() -> None:
        task = asyncio.create_task(hub._log_stats_periodically())
        await asyncio.sleep(0.05)
        clock.now += 5
        hub.tick()
        await asyncio.sleep(0.05)
        task.cancel()

    with caplog.at_level("INFO", logger="location_server.live.hub"):
        asyncio.run(scenario())
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("LIVE_STATS")]
    assert [line.split(",")[1] for line in lines] == ["active_sessions=1", "active_sessions=0"]


def test_session_id_of_ended_session_is_notified() -> None:
    """hello の無い短いセッションがコミットより先に終わっても、確定した ID を session_info で知らせる。"""
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    _drain(sub)
    hub.publish(make_packet(seq=0, flags=0x01))
    assert _types(_drain(sub)) == ["session_start", "append", "session_end"]
    hub.note_sessions({(1, 0xAAAAAAAA): 7})
    [info] = _drain(sub)
    assert info == {
        "type": "session_info",
        "session_id": 7,
        "tag_id": 1,
        "boot_id": 0xAAAAAAAA,
        "config_rev": None,
    }
    # 同じ ID を再び受けても送り直さない
    hub.note_sessions({(1, 0xAAAAAAAA): 7})
    assert _drain(sub) == []
    # 終了後の hello で構成リビジョンがわかった場合も知らせる
    hub.note_hello(1, 0xAAAAAAAA, 7, 3)
    [info] = _drain(sub)
    assert (info["type"], info["config_rev"]) == ("session_info", 3)


def test_active_session_id_arrives_by_append_only() -> None:
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    hub.publish(make_packet(seq=0))
    _drain(sub)
    hub.note_sessions({(1, 0xAAAAAAAA): 7})
    assert _drain(sub) == []


def _wide_packet(*, tag_id: int = 1, seq: int = 0, anchor_n: int = 255, count: int = 16) -> TelemetryPacket:
    return make_packet(
        tag_id=tag_id, seq=seq, count=count, anchors=tuple(0x0100 + i for i in range(anchor_n))
    )


def test_ring_buffer_limits_range_records_per_session() -> None:
    """anchor_n = 255 のパケットでも、1 セッションが抱える測距レコードは上限で打ち切る。"""
    hub, _ = _hub(max_buffer_ranges=1000)
    for i in range(10):
        hub.publish(_wide_packet(seq=i * 16))
    session = hub.sessions[1]
    assert session.range_count <= 1000
    assert session.range_count == sum(len(c.record.ranges) for c in session.cycles)
    assert hub.stats.overflow_cycles == 160 - len(session.cycles)


def test_total_range_records_are_limited_across_tags() -> None:
    """多数のタグ ID を名乗られても、全セッションの測距レコードの合計は上限に収まる。"""
    hub, _ = _hub(max_buffer_ranges=10_000, max_total_ranges=20_000)
    for tag_id in range(1, 30):
        hub.publish(_wide_packet(tag_id=tag_id, count=4))
    total = sum(session.range_count for session in hub.sessions.values())
    assert total <= 20_000
    # 最後に受けたタグのぶんは残る
    assert hub.sessions[29].range_count > 0


def test_total_limit_trims_ended_sessions_first() -> None:
    hub, clock = _hub(max_total_ranges=3000)
    hub.publish(_wide_packet(tag_id=1, count=8))  # 2040 件
    clock.now += 5
    hub.tick()  # タグ 1 は timeout で終了
    hub.publish(_wide_packet(tag_id=2, count=8))  # 合計 4080 件
    assert hub.sessions[2].range_count == 2040
    assert hub.sessions[1].range_count <= 3000 - 2040


def test_pending_cycles_dropped_by_limit_are_not_sent() -> None:
    hub, _ = _hub(max_buffer_ranges=255 * 4)
    sub = hub.connect()
    hub.subscribe(sub, 1)
    hub.publish(_wide_packet(seq=0, count=16))
    hub.tick()
    append = _drain(sub)[-1]
    assert append["fix"]["seq"] == [12, 13, 14, 15]


def test_live_unwrap_matches_query_api_at_boundaries() -> None:
    """境界の値でも、ライブの展開が参照 API (store/query.py) と同じ規則になる。"""
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    # seq の差がちょうど 2^31: 参照 API は折り返しとみなす (test_seq_wrap_boundary_is_half_range)
    hub.publish(make_packet(seq=2**31, t_tag_ms=1000, count=1))
    hub.publish(make_packet(seq=0, t_tag_ms=2000, count=1))
    hub.tick()
    append = [f for f in _drain(sub) if f["type"] == "append"][-1]
    assert append["fix"]["seq"] == [2**31, 2**32]
    assert hub.stats.late_cycles == 0


def test_live_time_forward_jump_is_not_treated_as_wrap() -> None:
    """時刻が 2^31 以上先へ飛んでも、参照 API と同じく生の差をそのまま足す。"""
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    hub.publish(make_packet(seq=0, t_tag_ms=1000, count=1))
    hub.tick()
    hub.publish(make_packet(seq=1, t_tag_ms=1000 + 2**31 + 5, count=1))
    hub.tick()
    appends = [f for f in _drain(sub) if f["type"] == "append"]
    assert [a["fix"]["t"] for a in appends] == [[1000], [1000 + 2**31 + 5]]
    assert appends[-1]["fix"]["dt"] == [2**31 + 5]


def test_session_info_is_not_sent_for_replaced_session() -> None:
    """置き換えられたセッションの ID が後からわかっても session_info は送らない (設計文書 5.6)。"""
    hub, _ = _hub()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    hub.publish(make_packet(boot_id=1, seq=0))
    hub.publish(make_packet(boot_id=2, seq=0))
    _drain(sub)
    hub.note_sessions({(1, 1): 7, (1, 2): 8})
    assert _drain(sub) == []
    assert hub.sessions[1].session_id == 8


def test_predict_only_cycle_keeps_filter_position() -> None:
    hub, _ = _hub()
    packet = make_packet(seq=0, count=1)
    cycle = replace(
        packet.cycles[0],
        fix_flags=FIX_FLAG_KF_OK,
        kf_x_mm=-1500,
        kf_y_mm=2500,
        kf_sigma_mm=65535,
        kf_used=0,
        kf_rejected=2,
    )
    hub.publish(replace(packet, cycles=(cycle,)))
    hub.tick()
    sub = hub.connect()
    hub.subscribe(sub, 1)
    [snapshot] = _drain(sub)
    fix = snapshot["fix"]
    assert (fix["ok"], fix["x"]) == ([False], [None])
    assert (fix["kok"], fix["kupd"], fix["kinit"]) == ([True], [False], [False])
    assert (fix["kx"], fix["ky"], fix["kz"], fix["ksig"]) == ([-1.5], [2.5], [1.0], [65.535])
    assert (fix["kused"], fix["krej"]) == ([0], [2])
