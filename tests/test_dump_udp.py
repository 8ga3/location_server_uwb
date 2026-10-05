"""`tools/dump_udp.py` の表示形式のテスト。"""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

import dump_udp
from location_server.ingest.packet import (
    COUNT_MAX,
    FIX_FLAG_KF_INIT,
    FIX_FLAG_KF_OK,
    FIX_FLAG_KF_UPDATED,
    FIX_FLAG_OK,
    RANGE_KF_ACCEPTED,
    RANGE_KF_REJECTED,
    RANGE_KF_UNUSED,
    TelemetryPacket,
    decode_packet,
    encode_packet,
    packet_size,
)
from telemetry_helpers import make_packet


def test_format_cycle() -> None:
    packet = decode_packet(encode_packet(make_packet(seq=5, t_tag_ms=1000, count=1)))
    line = dump_udp.format_cycle(packet, packet.cycles[0])
    assert line.startswith("tag_id=1,boot_id=0xAAAAAAAA,seq=5,t_ms=1000,fix=OK,x_mm=1234,y_mm=-5678,")
    assert ",0x0100=1000mm/6ms/kf_use," in line
    assert "last=1" not in line
    assert ",kf=UPDATE,kf_x_mm=1200,kf_y_mm=-5600,kf_z_mm=1000,kf_sigma_mm=35," in line
    assert ",kf_used=4,kf_rejected=0," in line
    assert "kf_init" not in line


def test_format_cycle_filter_states() -> None:
    packet = make_packet(count=1)
    cycle = packet.cycles[0]
    predicted = replace(cycle, fix_flags=FIX_FLAG_KF_OK, kf_used=0, kf_rejected=2)
    line = dump_udp.format_cycle(packet, predicted)
    assert ",fix=NG," in line
    assert ",kf=PREDICT,kf_x_mm=1200," in line
    assert ",kf_used=0,kf_rejected=2," in line
    init = replace(cycle, fix_flags=FIX_FLAG_OK | FIX_FLAG_KF_OK | FIX_FLAG_KF_UPDATED | FIX_FLAG_KF_INIT)
    assert ",kf_init=1," in dump_udp.format_cycle(packet, init)
    invalid = replace(cycle, fix_flags=FIX_FLAG_OK)
    line = dump_udp.format_cycle(packet, invalid)
    assert ",kf=NONE,kf_used=4,kf_rejected=0," in line
    assert "kf_x_mm" not in line


def test_format_cycle_range_kf() -> None:
    packet = make_packet(count=1)
    cycle = packet.cycles[0]
    kinds = (RANGE_KF_ACCEPTED, RANGE_KF_REJECTED, RANGE_KF_UNUSED, RANGE_KF_ACCEPTED)
    ranges = tuple(replace(r, kf=k) for r, k in zip(cycle.ranges, kinds, strict=True))
    line = dump_udp.format_cycle(packet, replace(cycle, ranges=ranges))
    # 使っていない測距には何も付けない
    assert (
        ",0x0100=1000mm/6ms/kf_use,0x0101=1100mm/6ms/kf_rej,0x0102=1200mm/6ms,0x0103=1300mm/6ms/kf_use"
        in line
    )


def test_fake_cycles_encode_and_stay_consistent() -> None:
    rng = random.Random(1)
    anchors = [0x0100 + i for i in range(4)]
    cycles = tuple(dump_udp._fake_cycle(seq, seq * 100, anchors, rng) for seq in range(16))
    packet = TelemetryPacket(flags=0, tag_id=1, boot_id=1, seq=0, t_tag_ms=0, anchor_n=4, cycles=cycles)
    decoded = decode_packet(encode_packet(packet))
    assert decoded.cycles[0].kf_init and decoded.cycles[0].kf_updated
    for cycle in decoded.cycles:
        assert cycle.kf_ok
        assert cycle.kf_used + cycle.kf_rejected <= sum(r.ok for r in cycle.ranges)
        if cycle.kf_init:
            # 最小二乗の解から初期化したサイクルでは、フィルタは測距を使っていない
            assert all(r.kf == RANGE_KF_UNUSED for r in cycle.ranges)
        else:
            assert sum(r.kf == RANGE_KF_ACCEPTED for r in cycle.ranges) == cycle.kf_used
            assert sum(r.kf == RANGE_KF_REJECTED for r in cycle.ranges) == cycle.kf_rejected
        # 失敗した測距はフィルタも使わない
        assert all(r.kf == RANGE_KF_UNUSED for r in cycle.ranges if not r.ok)
        if cycle.fix_ok:
            # フィルタの位置は最小二乗の解の近くにある
            assert abs(cycle.kf_x_mm - cycle.x_mm) < 1000
            assert abs(cycle.kf_y_mm - cycle.y_mm) < 1000


def test_fake_cycle_flags_follow_the_tag_rules() -> None:
    # 最小二乗は成功した測距が 3 本以上のときだけ解け、
    # フィルタは 1 本以上取り込めたときだけ観測で更新したことになる
    rng = random.Random(2)
    anchors = [0x0100 + i for i in range(4)]
    cycles = [dump_udp._fake_cycle(seq, seq * 100, anchors, rng) for seq in range(3000)]
    for cycle in cycles:
        used = sum(r.ok for r in cycle.ranges)
        assert cycle.used_count == used
        assert cycle.fix_ok == (used >= dump_udp.FAKE_MIN_RANGES)
        assert cycle.kf_updated == (cycle.kf_init or cycle.kf_used > 0)
        if not cycle.fix_ok:
            assert (cycle.x_mm, cycle.y_mm, cycle.residual_mm) == (0, 0, 0)
    # 予測だけの周期と、最小二乗は解けないがフィルタは観測で更新した周期の両方が現れる
    assert any(not c.kf_updated for c in cycles)
    assert any(not c.fix_ok and c.kf_updated for c in cycles)
    # 測距ごとの kf の本数はサイクルの取り込み数・棄却数と一致し、棄却した測距も現れる
    for cycle in cycles[1:]:
        assert sum(r.kf == RANGE_KF_ACCEPTED for r in cycle.ranges) == cycle.kf_used
        assert sum(r.kf == RANGE_KF_REJECTED for r in cycle.ranges) == cycle.kf_rejected
    assert any(r.kf == RANGE_KF_REJECTED for c in cycles for r in c.ranges)


def test_recv_buffer_holds_largest_valid_packet() -> None:
    # count と anchor_n を上限まで使ったパケットも切り捨てずに受け取れる
    assert packet_size(COUNT_MAX, 255) <= dump_udp.RECV_BUFFER


@pytest.mark.parametrize("anchor_n", [1, 2])
def test_fake_filter_stays_uninitialized_with_too_few_anchors(anchor_n: int) -> None:
    # 最小二乗が一度も解けないので、タグと同じくフィルタは初期化されず、無効のまま送る
    rng = random.Random(3)
    anchors = [0x0100 + i for i in range(anchor_n)]
    for seq in range(200):
        cycle = dump_udp._fake_cycle(seq, seq * 100, anchors, rng)
        assert not cycle.fix_ok
        assert not (cycle.kf_ok or cycle.kf_updated or cycle.kf_init)
        assert all(r.kf == RANGE_KF_UNUSED for r in cycle.ranges)
        assert (cycle.kf_x_mm, cycle.kf_y_mm, cycle.kf_sigma_mm, cycle.kf_used, cycle.kf_rejected) == (
            0,
            0,
            0,
            0,
            0,
        )
