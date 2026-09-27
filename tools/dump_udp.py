#!/usr/bin/env python3
"""テレメトリ UDP パケットを受信してデコードし、1 サイクル 1 行で表示する (設計文書 3.3)。

生の UDP バイナリは既製のツールでは中身が読めないため、タグ側の実装中にパケットを確かめる
手段としてこのダンパを置く。デコードはサーバー本体と同じ `location_server.ingest.packet` を使う。

サーバーと同じポートは同時に待ち受けられない。サーバーを止めてから使うか、構成配信の
`telemetry.port` を一時的にこのツールのポートへ向ける。

`send` はタグの代わりに擬似的なパケットを投げる。タグが手元に無いときにサーバー側の受信と
保存を確かめるために使う。

使用例:

    python tools/dump_udp.py listen --port 47100
    python tools/dump_udp.py send --host 127.0.0.1 --port 47100 --tag-id 1 --cycles 40
"""

from __future__ import annotations

import argparse
import random
import socket
import sys
import time

from location_server.ingest.packet import (
    FIX_FLAG_OK,
    CycleRecord,
    PacketDecodeError,
    RangeRecord,
    TelemetryPacket,
    decode_packet,
    encode_packet,
)
from location_server.settings import DEFAULT_UDP_PORT

RECV_BUFFER = 2048


def format_cycle(packet: TelemetryPacket, cycle: CycleRecord) -> str:
    """1 サイクルを 1 行の `key=value` 形式にする。ファームウェアのシリアルログと同じ書き方に揃える。"""
    fields = [
        f"tag_id={packet.tag_id}",
        f"boot_id=0x{packet.boot_id:08X}",
        f"seq={cycle.seq}",
        f"t_ms={cycle.t_tag_ms}",
        f"fix={'OK' if cycle.fix_ok else 'NG'}",
    ]
    if cycle.fix_ok:
        fields += [
            f"x_mm={cycle.x_mm}",
            f"y_mm={cycle.y_mm}",
            f"z_mm={cycle.z_mm}",
            f"dim={'3d' if cycle.fix_3d else '2d'}",
            f"resid_mm={cycle.residual_mm}",
        ]
    fields.append(f"used={cycle.used_count}")
    for r in cycle.ranges:
        value = f"{r.distance_mm}mm" if r.ok else f"err{r.status}"
        fields.append(f"0x{r.anchor_id:04X}={value}/{r.elapsed_ms}ms")
    if packet.last:
        fields.append("last=1")
    return ",".join(fields)


def listen(host: str, port: int) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    print(f"# listening on {host}:{port}", file=sys.stderr)
    # (tag_id, boot_id) ごとに次に来るはずの seq を覚え、欠番を表示する
    expected: dict[tuple[int, int], int] = {}
    try:
        while True:
            data, addr = sock.recvfrom(RECV_BUFFER)
            try:
                packet = decode_packet(data)
            except PacketDecodeError as exc:
                print(f"DROP,from={addr[0]}:{addr[1]},len={len(data)},reason={exc}", flush=True)
                continue
            key = (packet.tag_id, packet.boot_id)
            if key in expected and packet.seq != expected[key]:
                print(f"GAP,tag_id={packet.tag_id},expected={expected[key]},got={packet.seq}", flush=True)
            expected[key] = (packet.seq + len(packet.cycles)) & 0xFFFFFFFF
            for cycle in packet.cycles:
                print(format_cycle(packet, cycle), flush=True)
    except KeyboardInterrupt:
        return 0
    finally:
        sock.close()


def _fake_cycle(seq: int, t_ms: int, anchors: list[int], rng: random.Random) -> CycleRecord:
    """それらしい値を持つ擬似サイクル。1 割の確率で測距を失敗させる。"""
    ranges = []
    for anchor_id in anchors:
        if rng.random() < 0.1:
            ranges.append(RangeRecord(anchor_id, status=11, elapsed_ms=rng.randint(4, 8), distance_mm=0))
        else:
            ranges.append(
                RangeRecord(
                    anchor_id, status=0, elapsed_ms=rng.randint(4, 8), distance_mm=rng.randint(500, 6000)
                )
            )
    return CycleRecord(
        seq=seq,
        t_tag_ms=t_ms,
        fix_flags=FIX_FLAG_OK,
        used_count=sum(r.ok for r in ranges),
        x_mm=rng.randint(0, 5000),
        y_mm=rng.randint(0, 4000),
        z_mm=1000,
        residual_mm=rng.randint(10, 80),
        ranges=tuple(ranges),
    )


def send(host: str, port: int, tag_id: int, cycles: int, batch: int, period_ms: int, anchor_n: int) -> int:
    rng = random.Random()
    boot_id = rng.getrandbits(32)
    anchors = [0x0100 + i for i in range(anchor_n)]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    start = time.monotonic()
    seq = 0
    sent = 0
    try:
        while seq < cycles:
            n = min(batch, cycles - seq)
            base_t = seq * period_ms
            packet = TelemetryPacket(
                flags=0,
                tag_id=tag_id,
                boot_id=boot_id,
                seq=seq,
                t_tag_ms=base_t,
                anchor_n=anchor_n,
                cycles=tuple(_fake_cycle(seq + i, base_t + i * period_ms, anchors, rng) for i in range(n)),
            )
            sock.sendto(encode_packet(packet), (host, port))
            sent += 1
            seq += n
            # タグと同じく、束ねたサイクルぶんの時間が経ってから次を送る
            delay = start + seq * period_ms / 1000 - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    finally:
        sock.close()
    print(f"# sent {sent} packets ({cycles} cycles) tag_id={tag_id} boot_id=0x{boot_id:08X}", file=sys.stderr)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="テレメトリ UDP パケットのダンパと擬似送信")
    sub = parser.add_subparsers(dest="command", required=True)

    p_listen = sub.add_parser("listen", help="受信してデコード結果を表示する")
    p_listen.add_argument("--host", default="0.0.0.0")
    p_listen.add_argument("--port", type=int, default=DEFAULT_UDP_PORT)

    p_send = sub.add_parser("send", help="擬似パケットを送る")
    p_send.add_argument("--host", default="127.0.0.1")
    p_send.add_argument("--port", type=int, default=DEFAULT_UDP_PORT)
    p_send.add_argument("--tag-id", type=int, default=1)
    p_send.add_argument("--cycles", type=int, default=40, help="送るサイクル数の合計")
    p_send.add_argument("--batch", type=int, default=4, help="1 パケットに束ねるサイクル数 (1..16)")
    p_send.add_argument("--period-ms", type=int, default=200, help="1 サイクルの周期")
    p_send.add_argument("--anchors", type=int, default=4, help="1 サイクルあたりの測距本数")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "listen":
        return listen(args.host, args.port)
    return send(args.host, args.port, args.tag_id, args.cycles, args.batch, args.period_ms, args.anchors)


if __name__ == "__main__":
    raise SystemExit(main())
