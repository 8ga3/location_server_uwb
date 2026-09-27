"""受信したパケットを溜めて一括で DB へ書く保存系。

設計文書 6.3 のとおり、メモリ上のキューへ積み、100 ms ごとまたは 500 行たまるごとに
1 トランザクションで `INSERT` する。1 パケット 1 トランザクションにすると SQLite の fsync が
ボトルネックになるためである。

書き込みは別スレッドで行い、受信ループ (イベントループ) を DB の待ちで止めない。
DB が詰まってキューが上限を超えた場合は古いパケットから捨て、捨てた数を数える。
書き込みが失敗した束も再試行せずに捨てる。デバッグ用テレメトリにとって、欠測は
再試行で溜まり続けるメモリや遅延より安い。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass

from location_server.store import ReceivedPacket, TelemetryStore

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_S = 0.1
FLUSH_ROWS = 500
# DB が止まったときにメモリへ溜めてよい行数の上限。
# 最悪ケースの 400 行/秒 (設計文書 3.1) で 2 分強ぶんに当たる。
MAX_PENDING_ROWS = 50_000


@dataclass(slots=True)
class WriterStats:
    """保存系の累積カウンタ。"""

    written_rows: int = 0
    duplicate_rows: int = 0
    committed_batches: int = 0
    failed_batches: int = 0
    failed_rows: int = 0
    dropped_packets: int = 0
    dropped_rows: int = 0


class TelemetryWriter:
    """パケットのキューと、それを定期的に DB へ吐き出すタスク。

    `submit()` はイベントループのスレッドから呼ぶ前提で、ブロックしない。
    """

    def __init__(
        self,
        store: TelemetryStore,
        *,
        flush_interval_s: float = FLUSH_INTERVAL_S,
        flush_rows: int = FLUSH_ROWS,
        max_pending_rows: int = MAX_PENDING_ROWS,
    ) -> None:
        self._store = store
        self._flush_interval_s = flush_interval_s
        self._flush_rows = flush_rows
        self._max_pending_rows = max_pending_rows
        self._pending: deque[ReceivedPacket] = deque()
        self._pending_rows = 0
        self._wakeup = asyncio.Event()
        self._stopping = False
        self.stats = WriterStats()

    @property
    def pending_rows(self) -> int:
        return self._pending_rows

    def submit(self, item: ReceivedPacket) -> None:
        """パケットをキューへ積む。上限を超えたら古いものから捨てる。"""
        self._pending.append(item)
        self._pending_rows += item.packet.row_count
        while self._pending_rows > self._max_pending_rows and len(self._pending) > 1:
            dropped = self._pending.popleft()
            self._pending_rows -= dropped.packet.row_count
            self.stats.dropped_packets += 1
            self.stats.dropped_rows += dropped.packet.row_count
            if self.stats.dropped_packets == 1:
                # 溢れは DB が詰まっている兆候なので、最初の 1 回は必ずログへ残す。
                # 以降の件数は定期の統計ログ (INGEST_STATS) で追う。
                logger.warning(
                    "保存待ちのキューが上限 (%d 行) を超えたため古いパケットを捨てました",
                    self._max_pending_rows,
                )
        if self._pending_rows >= self._flush_rows:
            self._wakeup.set()

    def request_stop(self) -> None:
        """`run()` に終了を求める。`run()` は残りを書き切ってから戻る。"""
        self._stopping = True
        self._wakeup.set()

    async def run(self) -> None:
        """一定間隔、または行数が閾値に達するたびにキューを吐き出し続ける。

        止めるときはタスクを cancel せず、`request_stop()` を呼んでから終わるのを待つ。
        `asyncio.to_thread()` の待ちを cancel しても DB スレッドは止まらないので、
        書き込みの途中で接続を閉じてしまわないよう、書き込みの完了を必ずここで待つ。
        """
        while not self._stopping:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=self._flush_interval_s)
            self._wakeup.clear()
            await self.flush()
        # 停止を求められた時点でキューに残っているぶんを書き切る
        await self.flush()

    async def flush(self) -> None:
        """溜まっているぶんを 1 トランザクションで書き込む。"""
        if not self._pending:
            return
        batch = list(self._pending)
        batch_rows = self._pending_rows
        self._pending.clear()
        self._pending_rows = 0
        try:
            result = await asyncio.to_thread(self._store.write_packets, batch)
        except Exception:
            # 受信ループと保存タスクを止めないことを優先し、束ごと捨てて数える
            self.stats.failed_batches += 1
            self.stats.failed_rows += batch_rows
            logger.exception("テレメトリの書き込みに失敗したため %d 行を捨てました", batch_rows)
            return
        self.stats.committed_batches += 1
        self.stats.written_rows += result.rows
        self.stats.duplicate_rows += result.duplicate_rows
