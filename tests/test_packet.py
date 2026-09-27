"""UDP パケットのデコーダのテスト。設計文書 6.2 の形式と検証の境界を確認する。"""

from __future__ import annotations

import struct

import pytest

from location_server.ingest.packet import (
    CYCLE,
    HEADER,
    MAGIC,
    RANGE,
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
    version: int = 1,
    flags: int = 0,
    tag_id: int = 1,
    boot_id: int = 0x12345678,
    seq: int = 10,
    t_tag_ms: int = 5000,
    count: int = 1,
    anchor_n: int = 1,
) -> bytes:
    return HEADER.pack(magic, version, flags, tag_id, boot_id, seq, t_tag_ms, count, anchor_n, 0)


def _reason(data: bytes) -> DropReason:
    with pytest.raises(PacketDecodeError) as info:
        decode_packet(data)
    return info.value.reason


def test_sizes_match_design() -> None:
    assert HEADER.size == 24
    assert CYCLE.size == 16
    assert RANGE.size == 8
    # アンカー 4 台・4 サイクルで 216 バイト、count = 16 で 792 バイト (設計文書 6.2)
    assert packet_size(4, 4) == 216
    assert packet_size(16, 4) == 792


def test_magic_is_uwbt_in_little_endian() -> None:
    assert struct.pack("<I", MAGIC) == b"UWBT"


def test_decode_hand_built_packet() -> None:
    data = (
        _header(count=2, anchor_n=2, seq=100, t_tag_ms=7000)
        + CYCLE.pack(0, 0x03, 2, -1_000_000, 2_000_000, 1800, 65535)
        + RANGE.pack(0x0100, 0, 7, 3210)
        + RANGE.pack(0x0101, 11, 255, 0)
        + CYCLE.pack(50, 0x00, 0, 0, 0, 0, 0)
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
    assert not second.fix_ok
    assert first.ranges[0].ok and first.ranges[0].distance_mm == 3210
    assert not first.ranges[1].ok and first.ranges[1].status == 11 and first.ranges[1].elapsed_ms == 255
    assert packet.row_count == 6


def test_round_trip() -> None:
    packet = make_packet(count=16)
    assert decode_packet(encode_packet(packet)) == packet


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


def test_rejects_bad_version() -> None:
    assert _reason(_header(version=2) + b"\x00" * 24) is DropReason.BAD_VERSION


@pytest.mark.parametrize("tag_id", [0, 0x0100, 0xFFFF])
def test_rejects_out_of_range_tag_id(tag_id: int) -> None:
    assert _reason(_header(tag_id=tag_id) + b"\x00" * 24) is DropReason.BAD_TAG_ID


@pytest.mark.parametrize("count", [0, 17, 255])
def test_rejects_out_of_range_count(count: int) -> None:
    assert _reason(_header(count=count)) is DropReason.BAD_COUNT


def test_rejects_zero_anchor_n() -> None:
    assert _reason(_header(anchor_n=0) + CYCLE.pack(0, 0, 0, 0, 0, 0, 0)) is DropReason.BAD_ANCHOR_N


def test_rejects_length_mismatch() -> None:
    body = CYCLE.pack(0, 0, 0, 0, 0, 0, 0) + RANGE.pack(0x0100, 0, 0, 0)
    assert decode_packet(_header() + body).cycles
    assert _reason(_header() + body[:-1]) is DropReason.LENGTH_MISMATCH
    assert _reason(_header() + body + b"\x00") is DropReason.LENGTH_MISMATCH
    # 宣言した count / anchor_n と実際のレコード数の食い違い
    assert _reason(_header(count=2) + body) is DropReason.LENGTH_MISMATCH
    assert _reason(_header(anchor_n=2) + body) is DropReason.LENGTH_MISMATCH


@pytest.mark.parametrize("anchor_id", [0x0000, 0x00FF, 0xFFFF])
def test_rejects_out_of_range_anchor_id(anchor_id: int) -> None:
    data = _header() + CYCLE.pack(0, 0, 0, 0, 0, 0, 0) + RANGE.pack(anchor_id, 0, 0, 0)
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
    body = CYCLE.pack(0, 0, 0, 0, 0, 0, 0) + RANGE.pack(0x0100, 0, 0, 0)
    assert _reason(_header(flags=flags) + body) is DropReason.BAD_RESERVED


def test_rejects_nonzero_reserved() -> None:
    body = CYCLE.pack(0, 0, 0, 0, 0, 0, 0) + RANGE.pack(0x0100, 0, 0, 0)
    header = HEADER.pack(MAGIC, 1, 0, 1, 0, 0, 0, 1, 1, 0x0001)
    assert _reason(header + body) is DropReason.BAD_RESERVED
