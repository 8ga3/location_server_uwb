"""`tools/dump_udp.py` の表示形式のテスト。"""

from __future__ import annotations

import dump_udp
from location_server.ingest.packet import decode_packet, encode_packet
from telemetry_helpers import make_packet


def test_format_cycle() -> None:
    packet = decode_packet(encode_packet(make_packet(seq=5, t_tag_ms=1000, count=1)))
    line = dump_udp.format_cycle(packet, packet.cycles[0])
    assert line.startswith("tag_id=1,boot_id=0xAAAAAAAA,seq=5,t_ms=1000,fix=OK,x_mm=1234,y_mm=-5678,")
    assert "0x0100=1000mm/6ms" in line
    assert "last=1" not in line
