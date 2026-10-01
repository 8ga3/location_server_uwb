"""`tools/dump_udp.py` の表示形式のテスト。"""

from __future__ import annotations

import random
from dataclasses import replace

import dump_udp
from location_server.ingest.packet import (
    COUNT_MAX,
    FIX_FLAG_KF_INIT,
    FIX_FLAG_KF_OK,
    FIX_FLAG_KF_UPDATED,
    FIX_FLAG_OK,
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
    assert "0x0100=1000mm/6ms" in line
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
        if cycle.fix_ok:
            # フィルタの位置は最小二乗の解の近くにある
            assert abs(cycle.kf_x_mm - cycle.x_mm) < 1000
            assert abs(cycle.kf_y_mm - cycle.y_mm) < 1000
        else:
            assert not cycle.kf_updated


def test_recv_buffer_holds_largest_valid_packet() -> None:
    # count と anchor_n を上限まで使ったパケットも切り捨てずに受け取れる
    assert packet_size(COUNT_MAX, 255) <= dump_udp.RECV_BUFFER
