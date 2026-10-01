"""テレメトリ UDP パケットのデコードとエンコード。

形式は設計文書 6.2 に従う。すべてリトルエンディアンでパディングは入れない。
受け付けるのは version 2 だけである。version 2 はサイクルレコードに、タグ側のカルマンフィルタが出した
位置 (`kf_*`) を最小二乗の解と並べて載せる。タグとサーバーは同時に切り替える前提なので、
version 1 のパケットは `bad_version` として捨てる。
DB へ書き込む前にここで magic、version、長さ、各値の範囲を検証し、1 つでも外れたパケットは
理由つきの `PacketDecodeError` として丸ごと捨てる。部分的に読めたサイクルだけを拾うことはしない。

エンコーダはテストと結合確認用のツール (`tools/dump_udp.py` の送信モード) のために置いている。
タグ側の実装と同じ形式を Python 側でも 1 か所だけで定義しておくためである。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import StrEnum

from location_server.units import ANCHOR_ID_MAX, ANCHOR_ID_MIN, TAG_ID_MAX, TAG_ID_MIN

MAGIC = 0x54425755  # 'U' 'W' 'B' 'T' をリトルエンディアンで読んだ値
VERSION = 2

# ヘッダ: magic, version, flags, tag_id, boot_id, seq, t_tag_ms, count, anchor_n, reserved
HEADER = struct.Struct("<IBBHIIIBBH")
# サイクルレコード: dt_ms, fix_flags, used_count, x_mm, y_mm, z_mm, resid_mm,
#                   kf_x_mm, kf_y_mm, kf_z_mm, kf_sigma_mm, kf_used, kf_rejected
CYCLE = struct.Struct("<HBBiihHiihHBB")
# 測距レコード: anchor_id, status, elapsed_ms, distance_mm
RANGE = struct.Struct("<HBBi")

COUNT_MIN = 1
COUNT_MAX = 16
ANCHOR_N_MIN = 1

FLAG_LAST = 0x01  # このパケットが最後 (セッション終了)
FLAGS_KNOWN = FLAG_LAST  # version 2 で意味を持つビット。ほかは予約で 0
FIX_FLAG_OK = 0x01  # 最小二乗の測位成功
FIX_FLAG_3D = 0x02  # 3D 解
FIX_FLAG_KF_OK = 0x04  # フィルタの位置が有効
FIX_FLAG_KF_UPDATED = 0x08  # このサイクルで 1 本以上の観測によってフィルタを更新した (0 なら予測のみ)
FIX_FLAG_KF_INIT = 0x10  # このサイクルで最小二乗の解からフィルタを (再) 初期化した
# fix_flags で意味を持つビット。bit5..7 は予約で 0
FIX_FLAGS_KNOWN = FIX_FLAG_OK | FIX_FLAG_3D | FIX_FLAG_KF_OK | FIX_FLAG_KF_UPDATED | FIX_FLAG_KF_INIT

STATUS_OK = 0

_U32_MASK = 0xFFFFFFFF


class DropReason(StrEnum):
    """パケットを破棄した理由。受信側のカウンタのキーにもなる。"""

    TOO_SHORT = "too_short"
    BAD_MAGIC = "bad_magic"
    BAD_VERSION = "bad_version"
    BAD_RESERVED = "bad_reserved"
    BAD_TAG_ID = "bad_tag_id"
    BAD_COUNT = "bad_count"
    BAD_ANCHOR_N = "bad_anchor_n"
    LENGTH_MISMATCH = "length_mismatch"
    BAD_ANCHOR_ID = "bad_anchor_id"


class PacketDecodeError(ValueError):
    """パケットが設計文書 6.2 の形式に合わない場合に送出する。"""

    def __init__(self, reason: DropReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RangeRecord:
    """測距 1 本ぶん。`distance_mm` はタグが送った値そのままで、`status != 0` なら通常 0 が入る。"""

    anchor_id: int
    status: int
    elapsed_ms: int
    distance_mm: int

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


@dataclass(frozen=True, slots=True)
class CycleRecord:
    """1 サイクルぶんの測位結果と測距結果。`seq` と `t_tag_ms` は展開済みの絶対値を持つ。

    `x_mm` / `y_mm` / `z_mm` / `residual_mm` / `used_count` は最小二乗の解、`kf_*` はタグ側の
    カルマンフィルタの出力である。どちらもタグが送った値そのままで、ここでは補正しない。
    `kf_sigma_mm` は位置の標準偏差 (sqrt(Pxx + Pyy)) で 65535 で飽和する。`kf_used` / `kf_rejected` は
    このサイクルでフィルタが取り込んだ測距と、イノベーションのゲートで棄却した測距の本数である。
    """

    seq: int
    t_tag_ms: int
    fix_flags: int
    used_count: int
    x_mm: int
    y_mm: int
    z_mm: int
    residual_mm: int
    kf_x_mm: int
    kf_y_mm: int
    kf_z_mm: int
    kf_sigma_mm: int
    kf_used: int
    kf_rejected: int
    ranges: tuple[RangeRecord, ...]

    @property
    def fix_ok(self) -> bool:
        return bool(self.fix_flags & FIX_FLAG_OK)

    @property
    def fix_3d(self) -> bool:
        return bool(self.fix_flags & FIX_FLAG_3D)

    @property
    def kf_ok(self) -> bool:
        return bool(self.fix_flags & FIX_FLAG_KF_OK)

    @property
    def kf_updated(self) -> bool:
        return bool(self.fix_flags & FIX_FLAG_KF_UPDATED)

    @property
    def kf_init(self) -> bool:
        return bool(self.fix_flags & FIX_FLAG_KF_INIT)


@dataclass(frozen=True, slots=True)
class TelemetryPacket:
    """デコード済みのパケット。`seq` と `t_tag_ms` は先頭サイクルの値。"""

    flags: int
    tag_id: int
    boot_id: int
    seq: int
    t_tag_ms: int
    anchor_n: int
    cycles: tuple[CycleRecord, ...]

    @property
    def last(self) -> bool:
        return bool(self.flags & FLAG_LAST)

    @property
    def row_count(self) -> int:
        """DB へ書く行数 (測位 1 行 + 測距 anchor_n 行をサイクル数ぶん)。"""
        return len(self.cycles) * (1 + self.anchor_n)


def packet_size(count: int, anchor_n: int) -> int:
    """`count` サイクル・`anchor_n` 本のパケットが持つべきバイト数。"""
    return HEADER.size + count * (CYCLE.size + RANGE.size * anchor_n)


def decode_packet(data: bytes) -> TelemetryPacket:
    """UDP ペイロードを検証してデコードする。形式が合わなければ `PacketDecodeError` を送出する。"""
    if len(data) < HEADER.size:
        raise PacketDecodeError(DropReason.TOO_SHORT, f"{len(data)} バイトしかありません")

    magic, version, flags, tag_id, boot_id, seq, t_tag_ms, count, anchor_n, reserved = HEADER.unpack_from(
        data, 0
    )
    if magic != MAGIC:
        raise PacketDecodeError(DropReason.BAD_MAGIC, f"magic=0x{magic:08X}")
    if version != VERSION:
        raise PacketDecodeError(DropReason.BAD_VERSION, f"version={version}")
    # 予約ビットと予約欄は 0 と決めている。意味を足すときは version を上げる
    if flags & ~FLAGS_KNOWN or reserved != 0:
        raise PacketDecodeError(DropReason.BAD_RESERVED, f"flags=0x{flags:02X} reserved=0x{reserved:04X}")
    if not TAG_ID_MIN <= tag_id <= TAG_ID_MAX:
        raise PacketDecodeError(DropReason.BAD_TAG_ID, f"tag_id={tag_id}")
    if not COUNT_MIN <= count <= COUNT_MAX:
        raise PacketDecodeError(DropReason.BAD_COUNT, f"count={count}")
    if anchor_n < ANCHOR_N_MIN:
        raise PacketDecodeError(DropReason.BAD_ANCHOR_N, f"anchor_n={anchor_n}")
    expected = packet_size(count, anchor_n)
    if len(data) != expected:
        raise PacketDecodeError(
            DropReason.LENGTH_MISMATCH,
            f"count={count} anchor_n={anchor_n} なら {expected} バイトのはずが {len(data)} バイト",
        )

    cycles: list[CycleRecord] = []
    offset = HEADER.size
    for index in range(count):
        (
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
        ) = CYCLE.unpack_from(data, offset)
        offset += CYCLE.size
        if fix_flags & ~FIX_FLAGS_KNOWN:
            raise PacketDecodeError(DropReason.BAD_RESERVED, f"cycle={index} fix_flags=0x{fix_flags:02X}")
        ranges: list[RangeRecord] = []
        for _ in range(anchor_n):
            anchor_id, status, elapsed_ms, distance_mm = RANGE.unpack_from(data, offset)
            offset += RANGE.size
            if not ANCHOR_ID_MIN <= anchor_id <= ANCHOR_ID_MAX:
                raise PacketDecodeError(
                    DropReason.BAD_ANCHOR_ID, f"cycle={index} anchor_id=0x{anchor_id:04X}"
                )
            ranges.append(RangeRecord(anchor_id, status, elapsed_ms, distance_mm))
        cycles.append(
            CycleRecord(
                # seq と millis() はどちらも 32 ビットで折り返すので、展開後も同じ幅に揃える
                seq=(seq + index) & _U32_MASK,
                t_tag_ms=(t_tag_ms + dt_ms) & _U32_MASK,
                fix_flags=fix_flags,
                used_count=used_count,
                x_mm=x_mm,
                y_mm=y_mm,
                z_mm=z_mm,
                residual_mm=resid_mm,
                kf_x_mm=kf_x_mm,
                kf_y_mm=kf_y_mm,
                kf_z_mm=kf_z_mm,
                kf_sigma_mm=kf_sigma_mm,
                kf_used=kf_used,
                kf_rejected=kf_rejected,
                ranges=tuple(ranges),
            )
        )

    return TelemetryPacket(
        flags=flags,
        tag_id=tag_id,
        boot_id=boot_id,
        seq=seq,
        t_tag_ms=t_tag_ms,
        anchor_n=anchor_n,
        cycles=tuple(cycles),
    )


def encode_packet(packet: TelemetryPacket) -> bytes:
    """`TelemetryPacket` を設計文書 6.2 のバイト列へ戻す。

    各サイクルの `seq` は先頭からの連番、`t_tag_ms` は先頭から 0..65535 ms の範囲にある前提で、
    外れていれば `ValueError` を送出する。各フィールドの幅に収まらない値は `struct.error` になる。
    どちらの場合も範囲外の値を黙って丸めることはしない。
    """
    count = len(packet.cycles)
    if not COUNT_MIN <= count <= COUNT_MAX:
        raise ValueError(f"サイクル数は {COUNT_MIN}..{COUNT_MAX} です: {count}")

    parts = [
        HEADER.pack(
            MAGIC,
            VERSION,
            packet.flags,
            packet.tag_id,
            packet.boot_id,
            packet.seq,
            packet.t_tag_ms,
            count,
            packet.anchor_n,
            0,
        )
    ]
    for index, cycle in enumerate(packet.cycles):
        if cycle.seq != (packet.seq + index) & _U32_MASK:
            raise ValueError(f"cycle={index} の seq が連番になっていません: {cycle.seq}")
        if len(cycle.ranges) != packet.anchor_n:
            raise ValueError(f"cycle={index} の測距レコード数が anchor_n と一致しません")
        dt_ms = (cycle.t_tag_ms - packet.t_tag_ms) & _U32_MASK
        if dt_ms > 0xFFFF:
            raise ValueError(f"cycle={index} の t_tag_ms が先頭から 65535 ms を超えています")
        parts.append(
            CYCLE.pack(
                dt_ms,
                cycle.fix_flags,
                cycle.used_count,
                cycle.x_mm,
                cycle.y_mm,
                cycle.z_mm,
                cycle.residual_mm,
                cycle.kf_x_mm,
                cycle.kf_y_mm,
                cycle.kf_z_mm,
                cycle.kf_sigma_mm,
                cycle.kf_used,
                cycle.kf_rejected,
            )
        )
        parts.extend(RANGE.pack(r.anchor_id, r.status, r.elapsed_ms, r.distance_mm) for r in cycle.ranges)
    return b"".join(parts)
