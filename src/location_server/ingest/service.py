"""テレメトリ収集の起動と停止。

UDP ソケット、保存タスク、統計ログのタスクをまとめてアプリの寿命に合わせる。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import astuple

from location_server.ingest.udp import ReceiveStats, SeqTracker, TelemetryProtocol
from location_server.ingest.writer import TelemetryWriter
from location_server.store import TelemetryStore

logger = logging.getLogger(__name__)

STATS_LOG_INTERVAL_S = 10.0


class IngestService:
    """UDP 受信から DB への一括保存までを受け持つ。"""

    def __init__(self, store: TelemetryStore, *, stats_interval_s: float = STATS_LOG_INTERVAL_S) -> None:
        self.tracker = SeqTracker()
        self.receive_stats = ReceiveStats()
        self.writer = TelemetryWriter(store)
        self._stats_interval_s = stats_interval_s
        self._transport: asyncio.DatagramTransport | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._stats_task: asyncio.Task[None] | None = None

    @property
    def local_port(self) -> int | None:
        """実際に待ち受けているポート。ポート 0 を指定したテストで割り当てを知るために使う。"""
        if self._transport is None:
            return None
        port: int = self._transport.get_extra_info("sockname")[1]
        return port

    async def start(self, host: str, port: int) -> None:
        loop = asyncio.get_running_loop()
        protocol = TelemetryProtocol(self.writer.submit, self.tracker, self.receive_stats)
        transport, _ = await loop.create_datagram_endpoint(lambda: protocol, local_addr=(host, port))
        self._transport = transport
        self._writer_task = asyncio.create_task(self.writer.run(), name="telemetry-writer")
        self._stats_task = asyncio.create_task(self._log_stats_periodically(), name="telemetry-stats")
        logger.info("テレメトリの UDP 受信を開始しました: %s:%d", host, self.local_port)

    async def stop(self) -> None:
        """受信を止め、キューに残っているぶんを書き切ってから終わる。

        書き込みタスクは cancel せずに終了を求め、実行中の書き込みが終わるまで待つ。
        呼び出し側 (アプリの lifespan) はこのあとで SQLite の接続を閉じるので、
        ここで待たないと書き込みの途中で接続が閉じられる。
        """
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if self._stats_task is not None:
            self._stats_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stats_task
            self._stats_task = None
        if self._writer_task is not None:
            self.writer.request_stop()
            await self._writer_task
            self._writer_task = None
        else:
            await self.writer.flush()
        self.log_stats()

    def log_stats(self) -> None:
        """累積の統計を 1 行で出す。形式はファームウェアのシリアルログに合わせる。"""
        recv = self.receive_stats
        written = self.writer.stats
        dropped = ",".join(f"{reason.value}={n}" for reason, n in sorted(recv.dropped.items()))
        logger.info(
            "INGEST_STATS,accepted=%d,rejected=%d,lost_cycles=%d,late_cycles=%d,written_rows=%d,"
            "duplicate_rows=%d,failed_batches=%d,failed_rows=%d,dropped_packets=%d,pending_rows=%d%s",
            recv.accepted_packets,
            recv.dropped_packets,
            self.tracker.total_lost_cycles,
            self.tracker.total_late_cycles,
            written.written_rows,
            written.duplicate_rows,
            written.failed_batches,
            written.failed_rows,
            written.dropped_packets,
            self.writer.pending_rows,
            f",{dropped}" if dropped else "",
        )

    async def _log_stats_periodically(self) -> None:
        previous: tuple[object, ...] | None = None
        while True:
            await asyncio.sleep(self._stats_interval_s)
            # 何も受けていない間は同じ行を出し続けない
            current = (
                self.receive_stats.accepted_packets,
                self.receive_stats.dropped_packets,
                *astuple(self.writer.stats),
            )
            if current != previous:
                self.log_stats()
                previous = current
