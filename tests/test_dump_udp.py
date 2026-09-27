"""`tools/dump_udp.py` の表示形式のテスト。"""

from __future__ import annotations

import dump_udp
from location_server.ingest.packet import COUNT_MAX, decode_packet, encode_packet, packet_size
from telemetry_helpers import make_packet


def test_format_cycle() -> None:
    packet = decode_packet(encode_packet(make_packet(seq=5, t_tag_ms=1000, count=1)))
    line = dump_udp.format_cycle(packet, packet.cycles[0])
    assert line.startswith("tag_id=1,boot_id=0xAAAAAAAA,seq=5,t_ms=1000,fix=OK,x_mm=1234,y_mm=-5678,")
    assert "0x0100=1000mm/6ms" in line
    assert "last=1" not in line


def test_recv_buffer_holds_largest_valid_packet() -> None:
    # count と anchor_n を上限まで使ったパケットも切り捨てずに受け取れる
    assert packet_size(COUNT_MAX, 255) <= dump_udp.RECV_BUFFER
