"""UDP の受信と、セッションごとの欠番の追跡。

受信ループではデコードと欠番の計数だけを行い、DB には触れない。デコードに失敗したパケットは
理由ごとに数えて捨てる。不正なパケットで受信ループを止めないことを最優先にする。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from location_server.db import utc_now_text
from location_server.ingest.packet import DropReason, PacketDecodeError, TelemetryPacket, decode_packet
from location_server.store import ReceivedPacket

logger = logging.getLogger(__name__)

_U32_MASK = 0xFFFFFFFF
_HALF_RANGE = 1 << 31
# 欠番を追跡するセッションの数の上限。未認証の UDP から boot_id を変え続けられても、
# メモリが増え続けないようにする。溢れたら最も長く受信していないセッションから忘れる
MAX_TRACKED_SESSIONS = 1024


@dataclass(slots=True)
class SessionSeqState:
    """1 セッションぶんの受信状況。

    - `lost_cycles`: `seq` の欠番の累計。UDP で落ちたサイクル数の推定値
    - `late_cycles`: 期待より小さい `seq` で届いたサイクル数 (順序の入れ替わりか重複)

    遅れて届いたパケットで `lost_cycles` を減らすことはしない。重複と区別できないためで、
    正確な欠測率は DB に残った `seq` から後で数え直せる。
    """

    next_seq: int
    received_cycles: int = 0
    lost_cycles: int = 0
    late_cycles: int = 0
    last_seen_monotonic: float = 0.0


@dataclass(slots=True)
class SeqTracker:
    """`(tag_id, boot_id)` ごとに `seq` の連続性を見て欠番を数える。"""

    sessions: dict[tuple[int, int], SessionSeqState] = field(default_factory=dict)
    # 終了したセッションのぶんも含めた累計
    total_lost_cycles: int = 0
    total_late_cycles: int = 0
    # 上限を超えて忘れたセッションの数。忘れたセッションのパケットが再び届くと、新しいセッションとして数え直す
    evicted_sessions: int = 0
    max_sessions: int = MAX_TRACKED_SESSIONS
    clock: Callable[[], float] = time.monotonic

    def observe(self, packet: TelemetryPacket) -> int:
        """パケットを 1 つ記録し、今回新たに見つかった欠番の数を返す。"""
        key = (packet.tag_id, packet.boot_id)
        count = len(packet.cycles)
        state = self.sessions.get(key)
        new_lost = 0
        if state is None:
            state = SessionSeqState(next_seq=packet.seq)
            self.sessions[key] = state
            while len(self.sessions) > self.max_sessions:
                # dict は挿入順を保つ。受信のたびに末尾へ移すので、先頭が最も長く受信していない
                del self.sessions[next(iter(self.sessions))]
                self.evicted_sessions += 1
            logger.info(
                "テレメトリのセッションを検出しました: tag_id=%d boot_id=0x%08X seq=%d",
                packet.tag_id,
                packet.boot_id,
                packet.seq,
            )
        gap = (packet.seq - state.next_seq) & _U32_MASK
        if gap < _HALF_RANGE:
            # 期待どおりか、途中が抜けて先へ進んだ
            new_lost = gap
            state.lost_cycles += gap
            self.total_lost_cycles += gap
            state.next_seq = (packet.seq + count) & _U32_MASK
        else:
            state.late_cycles += count
            self.total_late_cycles += count
        state.received_cycles += count
        state.last_seen_monotonic = self.clock()
        if packet.last:
            logger.info(
                "タグがセッション終了を通知しました: tag_id=%d boot_id=0x%08X", packet.tag_id, packet.boot_id
            )
            del self.sessions[key]
        else:
            # 最後に受信した順に並べておく (上限を超えたときに先頭から忘れるため)
            self.sessions[key] = self.sessions.pop(key)
        return new_lost


@dataclass(slots=True)
class ReceiveStats:
    """受信系の累積カウンタ。"""

    accepted_packets: int = 0
    dropped: Counter[DropReason] = field(default_factory=Counter)

    @property
    def dropped_packets(self) -> int:
        return sum(self.dropped.values())


class TelemetryProtocol(asyncio.DatagramProtocol):
    """UDP パケットを受け取り、デコードして `sink` へ渡す。

    `sink` は受信ループのスレッドで呼ばれるので、ブロックしてはならない。
    """

    def __init__(
        self,
        sink: Callable[[ReceivedPacket], None],
        tracker: SeqTracker,
        stats: ReceiveStats | None = None,
    ) -> None:
        self._sink = sink
        self._tracker = tracker
        self.stats = stats if stats is not None else ReceiveStats()

    def datagram_received(self, data: bytes, addr: tuple[str | int, int]) -> None:
        try:
            packet = decode_packet(data)
        except PacketDecodeError as exc:
            self.stats.dropped[exc.reason] += 1
            # 理由ごとに最初の 1 回だけ警告し、以降は件数を定期の統計ログで追う。
            # 異種パケットを浴びたときにログが溢れないようにするため。
            level = logging.WARNING if self.stats.dropped[exc.reason] == 1 else logging.DEBUG
            logger.log(level, "不正な UDP パケットを捨てました (from %s): %s", addr, exc)
            return
        self.stats.accepted_packets += 1
        self._tracker.observe(packet)
        self._sink(ReceivedPacket(packet=packet, recv_at=utc_now_text()))

    def error_received(self, exc: Exception) -> None:
        logger.warning("UDP ソケットでエラーを受けました: %s", exc)
