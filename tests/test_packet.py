"""UDP パケットのデコーダのテスト。設計文書 6.2 の形式と検証の境界を確認する。"""

from __future__ import annotations

import struct
from dataclasses import replace

import pytest

from location_server.ingest.packet import (
    CYCLE,
    FIX_FLAG_KF_INIT,
    FIX_FLAG_KF_OK,
    FIX_FLAG_KF_UPDATED,
    FIX_FLAG_OK,
    FIX_FLAGS_KNOWN,
    HEADER,
    MAGIC,
    RANGE,
    VERSION,
    DropReason,
    PacketDecodeError,
    decode_packet,
    encode_packet,
    packet_size,
)
from telemetry_helpers import make_packet


def _header(
    *,
    magic: int = MAGIC,
    version: int = VERSION,
    flags: int = 0,
    tag_id: int = 1,
    boot_id: int = 0x12345678,
    seq: int = 10,
    t_tag_ms: int = 5000,
    count: int = 1,
    anchor_n: int = 1,
) -> bytes:
    return HEADER.pack(magic, version, flags, tag_id, boot_id, seq, t_tag_ms, count, anchor_n, 0)


def _cycle(
    *,
    dt_ms: int = 0,
    fix_flags: int = 0,
    used_count: int = 0,
    x_mm: int = 0,
    y_mm: int = 0,
    z_mm: int = 0,
    resid_mm: int = 0,
    kf_x_mm: int = 0,
    kf_y_mm: int = 0,
    kf_z_mm: int = 0,
    kf_sigma_mm: int = 0,
    kf_used: int = 0,
    kf_rejected: int = 0,
) -> bytes:
    return CYCLE.pack(
        dt_ms,
        fix_flags,
        used_count,
        x_mm,
        y_mm,
        z_mm,
        resid_mm,
        kf_x_mm,
        kf_y_mm,
        kf_z_mm,
        kf_sigma_mm,
        kf_used,
        kf_rejected,
    )


def _reason(data: bytes) -> DropReason:
    with pytest.raises(PacketDecodeError) as info:
        decode_packet(data)
    return info.value.reason


def test_sizes_match_design() -> None:
    assert VERSION == 2
    assert HEADER.size == 24
    assert CYCLE.size == 30
    assert RANGE.size == 8
    # アンカー 4 台・4 サイクルで 272 バイト、count = 16 で 1016 バイト (設計文書 6.2)
    assert packet_size(4, 4) == 272
    assert packet_size(16, 4) == 1016


def test_fix_flags_known_bits() -> None:
    assert FIX_FLAGS_KNOWN == 0x1F


def test_magic_is_uwbt_in_little_endian() -> None:
    assert struct.pack("<I", MAGIC) == b"UWBT"


def test_decode_hand_built_packet() -> None:
    data = (
        _header(count=2, anchor_n=2, seq=100, t_tag_ms=7000)
        + _cycle(
            fix_flags=0x1F,
            used_count=2,
            x_mm=-1_000_000,
            y_mm=2_000_000,
            z_mm=1800,
            resid_mm=65535,
            kf_x_mm=-999_000,
            kf_y_mm=1_999_000,
            kf_z_mm=-1800,
            kf_sigma_mm=65535,
            kf_used=1,
            kf_rejected=1,
        )
        + RANGE.pack(0x0100, 0, 7, 3210)
        + RANGE.pack(0x0101, 11, 255, 0)
        + _cycle(dt_ms=50, fix_flags=FIX_FLAG_KF_OK, kf_x_mm=1, kf_y_mm=2, kf_z_mm=3, kf_sigma_mm=80)
        + RANGE.pack(0x0100, 0, 6, 3200)
        + RANGE.pack(0x0101, 0, 6, 4100)
    )
    packet = decode_packet(data)
    assert (packet.tag_id, packet.boot_id, packet.seq, packet.anchor_n) == (1, 0x12345678, 100, 2)
    first, second = packet.cycles
    assert (first.seq, first.t_tag_ms) == (100, 7000)
    assert (second.seq, second.t_tag_ms) == (101, 7050)
    # 発散した座標や飽和値もそのまま取り出す
    assert first.fix_ok and first.fix_3d
    assert (first.x_mm, first.y_mm, first.z_mm, first.residual_mm) == (-1_000_000, 2_000_000, 1800, 65535)
    assert first.kf_ok and first.kf_updated and first.kf_init
    assert (first.kf_x_mm, first.kf_y_mm, first.kf_z_mm) == (-999_000, 1_999_000, -1800)
    assert (first.kf_sigma_mm, first.kf_used, first.kf_rejected) == (65535, 1, 1)
    assert not second.fix_ok
    # 最小二乗が解けず、フィルタが予測だけで進んだサイクル
    assert second.kf_ok and not second.kf_updated and not second.kf_init
    assert (second.kf_x_mm, second.kf_y_mm, second.kf_z_mm, second.kf_sigma_mm) == (1, 2, 3, 80)
    assert first.ranges[0].ok and first.ranges[0].distance_mm == 3210
    assert not first.ranges[1].ok and first.ranges[1].status == 11 and first.ranges[1].elapsed_ms == 255
    assert packet.row_count == 6


def test_round_trip() -> None:
    packet = make_packet(count=16)
    assert decode_packet(encode_packet(packet)) == packet


def test_round_trip_keeps_filter_fields() -> None:
    packet = make_packet(count=3)
    first, second, third = packet.cycles
    packet = replace(
        packet,
        cycles=(
            # 負の座標と、飽和した標準偏差
            replace(
                first,
                fix_flags=FIX_FLAG_OK | FIX_FLAG_KF_OK | FIX_FLAG_KF_UPDATED | FIX_FLAG_KF_INIT,
                kf_x_mm=-2_147_483_648,
                kf_y_mm=-1,
                kf_z_mm=-32768,
                kf_sigma_mm=65535,
                kf_used=255,
                kf_rejected=255,
            ),
            # 最小二乗が失敗し、フィルタは予測のみ
            replace(second, fix_flags=FIX_FLAG_KF_OK, kf_x_mm=2_147_483_647, kf_used=0, kf_rejected=3),
            # フィルタが無効
            replace(third, fix_flags=FIX_FLAG_OK, kf_x_mm=0, kf_y_mm=0, kf_z_mm=0, kf_sigma_mm=0),
        ),
    )
    decoded = decode_packet(encode_packet(packet))
    assert decoded == packet
    assert [(c.kf_ok, c.kf_updated, c.kf_init) for c in decoded.cycles] == [
        (True, True, True),
        (True, False, False),
        (False, False, False),
    ]
    assert decoded.cycles[0].kf_x_mm == -2_147_483_648
    assert decoded.cycles[0].kf_sigma_mm == 65535


def test_encode_produces_version_2() -> None:
    data = encode_packet(make_packet(count=1))
    assert data[4] == 2
    assert len(data) == packet_size(1, 4)


def test_seq_and_time_wrap_around() -> None:
    packet = make_packet(seq=0xFFFFFFFF, t_tag_ms=0xFFFFFFF0, count=2, period_ms=0x20)
    decoded = decode_packet(encode_packet(packet))
    assert [c.seq for c in decoded.cycles] == [0xFFFFFFFF, 0]
    assert [c.t_tag_ms for c in decoded.cycles] == [0xFFFFFFF0, 0x10]


def test_last_flag() -> None:
    assert decode_packet(encode_packet(make_packet(flags=0x01))).last
    assert not decode_packet(encode_packet(make_packet(flags=0x00))).last


def test_rejects_short_packet() -> None:
    assert _reason(b"") is DropReason.TOO_SHORT
    assert _reason(_header()[:23]) is DropReason.TOO_SHORT


def test_rejects_bad_magic() -> None:
    assert _reason(_header(magic=0x12345678) + b"\x00" * 24) is DropReason.BAD_MAGIC


@pytest.mark.parametrize("version", [0, 1, 3, 255])
def test_rejects_bad_version(version: int) -> None:
    assert _reason(_header(version=version) + b"\x00" * 24) is DropReason.BAD_VERSION


def test_rejects_version_1_packet() -> None:
    # version 1 のサイクルレコード (16 バイト) で組んだパケットは、長さを見る前に version で捨てる
    v1_cycle = struct.pack("<HBBiihH", 0, 0x01, 1, 1000, 2000, 1000, 42)
    data = _header(version=1) + v1_cycle + RANGE.pack(0x0100, 0, 6, 1000)
    assert _reason(data) is DropReason.BAD_VERSION


@pytest.mark.parametrize("tag_id", [0, 0x0100, 0xFFFF])
def test_rejects_out_of_range_tag_id(tag_id: int) -> None:
    assert _reason(_header(tag_id=tag_id) + b"\x00" * 24) is DropReason.BAD_TAG_ID


@pytest.mark.parametrize("count", [0, 17, 255])
def test_rejects_out_of_range_count(count: int) -> None:
    assert _reason(_header(count=count)) is DropReason.BAD_COUNT


def test_rejects_zero_anchor_n() -> None:
    assert _reason(_header(anchor_n=0) + _cycle()) is DropReason.BAD_ANCHOR_N


def test_rejects_length_mismatch() -> None:
    body = _cycle() + RANGE.pack(0x0100, 0, 0, 0)
    assert len(body) == 30 + 8
    assert decode_packet(_header() + body).cycles
    assert _reason(_header() + body[:-1]) is DropReason.LENGTH_MISMATCH
    assert _reason(_header() + body + b"\x00") is DropReason.LENGTH_MISMATCH
    # 宣言した count / anchor_n と実際のレコード数の食い違い
    assert _reason(_header(count=2) + body) is DropReason.LENGTH_MISMATCH
    assert _reason(_header(anchor_n=2) + body) is DropReason.LENGTH_MISMATCH
    # version 1 の長さ (16 バイトのサイクルレコード) では足りない
    assert _reason(_header() + body[:16] + body[30:]) is DropReason.LENGTH_MISMATCH


@pytest.mark.parametrize("anchor_id", [0x0000, 0x00FF, 0xFFFF])
def test_rejects_out_of_range_anchor_id(anchor_id: int) -> None:
    data = _header() + _cycle() + RANGE.pack(anchor_id, 0, 0, 0)
    assert _reason(data) is DropReason.BAD_ANCHOR_ID


def test_encode_rejects_non_consecutive_seq() -> None:
    packet = make_packet(count=2)
    broken = type(packet)(
        flags=packet.flags,
        tag_id=packet.tag_id,
        boot_id=packet.boot_id,
        seq=packet.seq,
        t_tag_ms=packet.t_tag_ms,
        anchor_n=packet.anchor_n,
        cycles=(packet.cycles[0], packet.cycles[0]),
    )
    with pytest.raises(ValueError, match="seq"):
        encode_packet(broken)


@pytest.mark.parametrize("flags", [0x02, 0x80, 0xFF])
def test_rejects_unknown_flags(flags: int) -> None:
    body = _cycle() + RANGE.pack(0x0100, 0, 0, 0)
    assert _reason(_header(flags=flags) + body) is DropReason.BAD_RESERVED


def test_rejects_nonzero_reserved() -> None:
    body = _cycle() + RANGE.pack(0x0100, 0, 0, 0)
    header = HEADER.pack(MAGIC, VERSION, 0, 1, 0, 0, 0, 1, 1, 0x0001)
    assert _reason(header + body) is DropReason.BAD_RESERVED


@pytest.mark.parametrize("fix_flags", [0x20, 0x40, 0x80, 0xFF])
def test_rejects_reserved_fix_flags(fix_flags: int) -> None:
    good = _cycle(fix_flags=FIX_FLAGS_KNOWN) + RANGE.pack(0x0100, 0, 0, 0)
    bad = _cycle(dt_ms=50, fix_flags=fix_flags) + RANGE.pack(0x0100, 0, 0, 0)
    assert decode_packet(_header() + good).cycles
    # 2 番目のサイクルだけが予約ビットを立てていても、パケットを丸ごと捨てる
    with pytest.raises(PacketDecodeError) as info:
        decode_packet(_header(count=2) + good + bad)
    assert info.value.reason is DropReason.BAD_RESERVED
    assert f"cycle=1 fix_flags=0x{fix_flags:02X}" in info.value.detail
