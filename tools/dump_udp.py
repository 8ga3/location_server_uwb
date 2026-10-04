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
import math
import random
import socket
import sys
import time

from location_server.ingest.packet import (
    FIX_FLAG_KF_INIT,
    FIX_FLAG_KF_OK,
    FIX_FLAG_KF_UPDATED,
    FIX_FLAG_OK,
    CycleRecord,
    PacketDecodeError,
    RangeRecord,
    TelemetryPacket,
    decode_packet,
    encode_packet,
)
from location_server.settings import DEFAULT_UDP_PORT

# UDP データグラムの最大長。形式上有効な最大のパケット (count=16, anchor_n=255) は 32,920 バイトあり、
# それより小さいバッファでは recvfrom() が末尾を切り捨て、有効なパケットを length_mismatch と誤表示する
RECV_BUFFER = 65535

# 擬似送信でタグを動かす円軌道。中心 [mm]、半径 [mm]、1 周の時間 [ms]
FAKE_CENTER_MM = (2500, 2000)
FAKE_RADIUS_MM = 1500
FAKE_LAP_MS = 20_000
FAKE_TAG_Z_MM = 1000
# 最小二乗が解けるのに要る測距の本数 (タグの TRILAT_MIN_RANGES と同じ)
FAKE_MIN_RANGES = 3


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
    # フィルタの状態はファームウェアの POS_KF 行と同じ呼び方 (NONE / UPDATE / PREDICT) にする
    if not cycle.kf_ok:
        fields.append("kf=NONE")
    else:
        fields += [
            f"kf={'UPDATE' if cycle.kf_updated else 'PREDICT'}",
            f"kf_x_mm={cycle.kf_x_mm}",
            f"kf_y_mm={cycle.kf_y_mm}",
            f"kf_z_mm={cycle.kf_z_mm}",
            f"kf_sigma_mm={cycle.kf_sigma_mm}",
        ]
        if cycle.kf_init:
            fields.append("kf_init=1")
    fields += [f"kf_used={cycle.kf_used}", f"kf_rejected={cycle.kf_rejected}"]
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
    """それらしい値を持つ擬似サイクル。

    タグは円軌道を一定の速さで回るものとし、最小二乗の解は真の位置に数十 mm のばらつきを、
    フィルタの位置はそれより小さいばらつきを持たせる。1 割の確率で測距を失敗させ、5% の確率で全アンカーの
    測距を失敗させる。フラグはタグと同じ規則で決める。最小二乗は成功した測距が 3 本以上のときだけ解け、
    フィルタは取り込めた測距が 1 本以上あれば観測で更新したことにし、無ければ予測だけで進んだことにする。
    先頭のサイクル (`seq = 0`) は全アンカーの測距を成功させ、最小二乗の解からフィルタを初期化したことにする。
    """
    init = seq == 0
    blackout = not init and rng.random() < 0.05
    ranges = []
    for anchor_id in anchors:
        if blackout or (not init and rng.random() < 0.1):
            ranges.append(RangeRecord(anchor_id, status=11, elapsed_ms=rng.randint(4, 8), distance_mm=0))
        else:
            ranges.append(
                RangeRecord(
                    anchor_id, status=0, elapsed_ms=rng.randint(4, 8), distance_mm=rng.randint(500, 6000)
                )
            )
    used = sum(r.ok for r in ranges)
    ls_ok = used >= FAKE_MIN_RANGES
    angle = 2 * math.pi * (t_ms % FAKE_LAP_MS) / FAKE_LAP_MS
    true_x = FAKE_CENTER_MM[0] + FAKE_RADIUS_MM * math.cos(angle)
    true_y = FAKE_CENTER_MM[1] + FAKE_RADIUS_MM * math.sin(angle)
    ls_x = round(true_x + rng.gauss(0, 60))
    ls_y = round(true_y + rng.gauss(0, 60))

    fix_flags = FIX_FLAG_KF_OK
    if ls_ok:
        fix_flags |= FIX_FLAG_OK
    if init:
        # 初期化した周期の kf_used は、初期化に使った最小二乗の本数 (タグと同じ)
        kf_used = used
        kf_rejected = 0
        kf_sigma = rng.randint(150, 250)
    else:
        # 取り込めた測距のうち、たまに 1 本をゲートで棄却する
        kf_rejected = 1 if used > 0 and rng.random() < 0.05 else 0
        kf_used = used - kf_rejected
        kf_sigma = rng.randint(20, 60) if kf_used > 0 else rng.randint(60, 120)
    if init or kf_used > 0:
        fix_flags |= FIX_FLAG_KF_UPDATED
    if init:
        fix_flags |= FIX_FLAG_KF_INIT
        kf_x, kf_y = ls_x, ls_y
    else:
        kf_x = round(true_x + rng.gauss(0, 25))
        kf_y = round(true_y + rng.gauss(0, 25))
    return CycleRecord(
        seq=seq,
        t_tag_ms=t_ms,
        fix_flags=fix_flags,
        used_count=used,
        # 最小二乗が解けなかったサイクルは、タグと同じく座標欄を 0 のまま送る
        x_mm=ls_x if ls_ok else 0,
        y_mm=ls_y if ls_ok else 0,
        z_mm=FAKE_TAG_Z_MM,
        residual_mm=rng.randint(10, 80) if ls_ok else 0,
        kf_x_mm=kf_x,
        kf_y_mm=kf_y,
        kf_z_mm=FAKE_TAG_Z_MM,
        kf_sigma_mm=kf_sigma,
        kf_used=kf_used,
        kf_rejected=kf_rejected,
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
