"""ライブ配信 (設計文書 5.6 / 6.3 / 8.3)。

受信直後のパース結果を、タグごとのリングバッファ (直近 60 秒) へ入れて WebSocket の購読者へ配る。
DB には一切触れない。保存系が詰まってもライブ表示を止めないためで、`session_id` のように DB で
決まる値は、保存系のコミットや hello から後で教えてもらう。

イベントループのスレッドだけから呼ぶ前提で、ロックは持たない。受信ループ (`publish`)、
定期処理 (`tick`)、WebSocket の各接続がすべて同じループの上で動く。

購読者へ送るフレームは次の 6 種類。`snapshot` と `session_start` の `anchors` は、
DB を読む必要があるので WebSocket の接続ごとの送信処理で足す (`routes_live`)。

- `snapshot`: 購読した直後に 1 回だけ、直近 `history_ms` ぶんをまとめて送る
- `append`: 以降の差分。`tick()` ごと (50 ms) にまとめて送る
- `session_start` / `session_end`: セッションの開始と終了
- `session_info`: 終了済みのセッションの `session_id` / `config_rev` が後からわかったときの通知。
  稼働中のセッションなら append (と hello 後の `session_start`) に載るので送らない
- `error`: 購読要求の誤り
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import astuple, dataclass, field
from typing import Any

from location_server.columns import FixColumns, RangeTable
from location_server.ingest.packet import CycleRecord, TelemetryPacket

logger = logging.getLogger(__name__)

Frame = dict[str, Any]

# 1 セッションぶんのリングバッファが保持する時間幅 (設計文書 6.3)
RETENTION_MS = 60_000
# 時間幅とは別に持つ件数の上限。タグの millis() が飛んだ場合でもメモリを食い続けないようにする。
# 30 Hz (32 ms 周期) の 60 秒ぶん (約 1900 サイクル) に対して 2 倍の余裕を持たせた
MAX_BUFFER_CYCLES = 4096
# 差分を束ねて送る間隔 (設計文書 5.6)
FLUSH_INTERVAL_S = 0.05
# この時間パケットが来なければセッション終了とみなす (設計文書 8.3)
SESSION_TIMEOUT_S = 3.0
# snapshot で返せる履歴の上限と既定値
HISTORY_MS_MAX = RETENTION_MS
HISTORY_MS_DEFAULT = 30_000
# 購読者ごとのフレームキューの上限。50 ms ごとの append で約 1 秒ぶん。
# ライブ表示で見たいのは最新の状態なので、これより遅れたクライアントには古いフレームを捨てて追いつかせる
SUBSCRIBER_MAX_FRAMES = 20
# 捨てられない制御フレームが溜まり続けた場合のハード上限 (SUBSCRIBER_MAX_FRAMES の倍数)。
# これを超えたクライアントは切断する。ページは再接続して snapshot から取り直す
SUBSCRIBER_HARD_LIMIT_FACTOR = 2
STATS_LOG_INTERVAL_S = 10.0

# 終了済みとして覚えておく boot_id の数 (タグごと)。再起動後に遅れて届いた旧セッションのパケットを
# 新しいセッションと取り違えないようにするため
ENDED_BOOTS_PER_TAG = 8
# hello で知った (tag_id, boot_id) の対応を覚えておく数。UDP より先に hello が届く通常の順序に備える
HELLO_MEMORY = 256

END_TIMEOUT = "timeout"
END_NEW_SESSION = "new_session"
END_TAG_LAST = "tag_end"

_U32_MASK = 0xFFFFFFFF
_HALF_RANGE = 1 << 31


def _ms_after(later: int, earlier: int) -> int:
    """`millis()` の 32 ビット折り返しを考慮した `later - earlier`。負になる場合は負を返す。"""
    diff = (later - earlier) & _U32_MASK
    return diff if diff < _HALF_RANGE else diff - (1 << 32)


@dataclass(slots=True)
class LiveStats:
    """ライブ系の累積カウンタ。定期的に `LIVE_STATS,...` としてログへ出す。

    - `late_cycles`: 直前に受けたものより新しくない `seq` で届いたため捨てたサイクル (順序の入れ替わり・重複)
    - `stale_cycles`: 終了済みのセッションに属するため捨てたサイクル
    - `overflow_cycles`: リングバッファの件数上限を超えて捨てたサイクル
    - `dropped_frames`: 購読者のキューが溢れて捨てたフレームと、新しい `error` に置き換えたフレーム
      (全購読者の合計)
    - `overflow_disconnects`: 制御フレームだけでキューのハード上限を超えたため切断した接続の数
    """

    published_cycles: int = 0
    late_cycles: int = 0
    stale_cycles: int = 0
    overflow_cycles: int = 0
    dropped_frames: int = 0
    overflow_disconnects: int = 0


@dataclass(frozen=True, slots=True)
class LiveCycle:
    """リングバッファの 1 要素。

    `t_ms` / `seq` は 32 ビットの折り返しを展開した値で、セッションの中で単調に増える。
    周期 (`dt_ms`) も受信した時点で直前のサイクルから求めておく。
    """

    record: CycleRecord
    t_ms: int
    seq: int
    dt_ms: int | None


@dataclass(slots=True)
class LiveSession:
    """タグ 1 台の現在のセッション。終了後も、次のセッションが始まるまでは最後の状態を残す。"""

    tag_id: int
    boot_id: int
    session_id: int | None
    config_rev: int | None
    last_seen: float
    active: bool = True
    cycles: deque[LiveCycle] = field(default_factory=deque)
    pending: list[LiveCycle] = field(default_factory=list)
    # 直前に受け入れたサイクルの生の値と、折り返しを展開した値
    last_seq: int | None = None
    last_t_raw: int = 0
    last_seq_n: int = 0
    last_t_n: int = 0

    def identity(self) -> Frame:
        return {"session_id": self.session_id, "tag_id": self.tag_id, "boot_id": self.boot_id}


def columns_of(cycles: Iterable[LiveCycle]) -> tuple[FixColumns, RangeTable]:
    """サイクル列を列指向の測位・測距データへ直す。"""
    fix = FixColumns()
    ranges = RangeTable()
    for cycle in cycles:
        fix.add_cycle(cycle.record, t_ms=cycle.t_ms, seq=cycle.seq, dt_ms=cycle.dt_ms)
        ranges.add_cycle(cycle.record, t_ms=cycle.t_ms, seq=cycle.seq)
    return fix, ranges


class SubscriberOverflowError(Exception):
    """捨てられないフレームだけでキューのハード上限を超えた。接続を切って取り直させる。"""


class Subscriber:
    """WebSocket 1 接続ぶんの購読状態と、上限つきのフレームキュー。"""

    def __init__(self, *, max_frames: int = SUBSCRIBER_MAX_FRAMES, stats: LiveStats | None = None) -> None:
        self.tag_id: int | None = None
        # 購読してから捨てたフレームの累計。append の `lost` として通知する
        self.lost = 0
        self._max_frames = max_frames
        self._hard_limit = max_frames * SUBSCRIBER_HARD_LIMIT_FACTOR
        self.overflowed = False
        self._frames: deque[Frame] = deque()
        self._ready = asyncio.Event()
        self._stats = stats

    @property
    def pending_frames(self) -> int:
        return len(self._frames)

    def push(self, frame: Frame) -> None:
        """フレームを積む。上限を超えたら古い append から捨てる。

        - `error` は未送信のものを 1 つだけ残し、新しいものに置き換える。誤った要求を連打されても溜まらない
        - `snapshot` / `session_start` / `session_end` / `session_info` は捨てない。落とすとクライアントの状態が
          食い違うため
        - それでもハード上限 (`max_frames` の 2 倍) を超えたら、キューを捨てて `overflowed` を立てる。
          送信側は接続を閉じ、ページは再接続して snapshot から取り直す。偽の UDP パケットで
          `session_start` / `session_end` を大量に起こされても、接続ごとのメモリが有限に収まる
        """
        if self.overflowed:
            return
        if frame["type"] == "error":
            self._drop_pending_errors()
        self._frames.append(frame)
        while len(self._frames) > self._max_frames:
            if not self._drop_oldest_append():
                break
        if len(self._frames) > self._hard_limit:
            self._frames.clear()
            self.overflowed = True
            if self._stats is not None:
                self._stats.overflow_disconnects += 1
        self._ready.set()

    def reset(self, first: Frame | None) -> None:
        """購読の切り替え。積んであるフレームを捨てて `first` (snapshot) だけにする。"""
        self._frames.clear()
        self.lost = 0
        if first is not None:
            self.push(first)

    def pop_nowait(self) -> Frame | None:
        if not self._frames:
            self._ready.clear()
            return None
        return self._frames.popleft()

    async def next(self) -> Frame:
        """次のフレームを待つ。キューがハード上限を超えていれば `SubscriberOverflowError` を送出する。"""
        while True:
            if self.overflowed:
                raise SubscriberOverflowError
            frame = self.pop_nowait()
            if frame is not None:
                return frame
            await self._ready.wait()

    def _drop_pending_errors(self) -> None:
        kept = deque(frame for frame in self._frames if frame["type"] != "error")
        dropped = len(self._frames) - len(kept)
        if dropped and self._stats is not None:
            self._stats.dropped_frames += dropped
        self._frames = kept

    def _drop_oldest_append(self) -> bool:
        for index, frame in enumerate(self._frames):
            if frame["type"] == "append":
                del self._frames[index]
                self.lost += 1
                if self._stats is not None:
                    self._stats.dropped_frames += 1
                return True
        return False


class LiveHub:
    """タグごとのリングバッファと購読者を束ねる。"""

    def __init__(
        self,
        *,
        retention_ms: int = RETENTION_MS,
        max_buffer_cycles: int = MAX_BUFFER_CYCLES,
        flush_interval_s: float = FLUSH_INTERVAL_S,
        session_timeout_s: float = SESSION_TIMEOUT_S,
        subscriber_max_frames: int = SUBSCRIBER_MAX_FRAMES,
        stats_interval_s: float = STATS_LOG_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._retention_ms = retention_ms
        self._max_buffer_cycles = max_buffer_cycles
        self._flush_interval_s = flush_interval_s
        self._session_timeout_s = session_timeout_s
        self._subscriber_max_frames = subscriber_max_frames
        self._stats_interval_s = stats_interval_s
        self._clock = clock
        self.sessions: dict[int, LiveSession] = {}
        self._ended_boots: dict[int, deque[int]] = {}
        self._hello: OrderedDict[tuple[int, int], tuple[int, int | None]] = OrderedDict()
        self._subscribers: set[Subscriber] = set()
        self.stats = LiveStats()

    # ------------------------------------------------------------ 購読者

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def connect(self) -> Subscriber:
        subscriber = Subscriber(max_frames=self._subscriber_max_frames, stats=self.stats)
        self._subscribers.add(subscriber)
        return subscriber

    def disconnect(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)

    def subscribe(self, subscriber: Subscriber, tag_id: int, history_ms: int = HISTORY_MS_DEFAULT) -> None:
        """購読対象を `tag_id` へ切り替え、直近 `history_ms` ぶんの snapshot を積む。

        snapshot の作成と購読の登録は await を挟まずに行うので、snapshot 以降の差分は漏れなく届く。
        """
        subscriber.tag_id = tag_id
        subscriber.reset(self.snapshot(tag_id, history_ms))

    def unsubscribe(self, subscriber: Subscriber) -> None:
        subscriber.tag_id = None
        subscriber.reset(None)

    def snapshot(self, tag_id: int, history_ms: int = HISTORY_MS_DEFAULT) -> Frame:
        """タグの現在のセッションの直近 `history_ms` ぶん。セッションが無ければ空の snapshot を返す。

        まだ差分として送っていない (`pending` の) サイクルは含めない。それらは購読の登録後に送る
        最初の append で届くので、snapshot と append の間で抜けも重複も起きない。
        """
        session = self.sessions.get(tag_id)
        if session is None:
            fix, ranges = columns_of(())
            return {
                "type": "snapshot",
                "session_id": None,
                "tag_id": tag_id,
                "boot_id": None,
                "config_rev": None,
                "active": False,
                "history_ms": history_ms,
                "fix": fix.to_json(),
                "ranges": ranges.to_json(),
            }
        sent = list(session.cycles)[: max(0, len(session.cycles) - len(session.pending))]
        latest = sent[-1].t_ms if sent else 0
        recent = [c for c in sent if latest - c.t_ms <= history_ms]
        fix, ranges = columns_of(recent)
        return {
            "type": "snapshot",
            **session.identity(),
            "config_rev": session.config_rev,
            "active": session.active,
            "history_ms": history_ms,
            "fix": fix.to_json(),
            "ranges": ranges.to_json(),
        }

    # ------------------------------------------------------------ 受信側

    def publish(self, packet: TelemetryPacket) -> None:
        """受信直後のパケットをリングバッファへ入れる。差分の配信は次の `tick()` で行う。"""
        now = self._clock()
        tag_id = packet.tag_id
        if packet.boot_id in self._ended_boots.get(tag_id, ()):
            self.stats.stale_cycles += len(packet.cycles)
            return

        session = self.sessions.get(tag_id)
        if session is None or session.boot_id != packet.boot_id:
            if session is not None:
                self._end(session, END_NEW_SESSION)
            session = self._start(tag_id, packet.boot_id, now)
        elif not session.active:
            # 3 秒以上途切れたあとに同じ起動のまま戻ってきた (Wi-Fi の再接続など)。
            # 同じセッションとして再開する
            session.active = True
            self._broadcast(tag_id, self._session_start_frame(session))

        for cycle in packet.cycles:
            if session.last_seq is not None and _ms_after(cycle.seq, session.last_seq) <= 0:
                # ライブ表示は最新を優先する。遅れて届いたサイクルは軌跡の途中へ差し込まずに捨てる。
                # DB 側には保存系が別に書くので、再生では抜けずに見える
                self.stats.late_cycles += 1
                continue
            if session.last_seq is None:
                seq_n, t_n, dt_ms = cycle.seq, cycle.t_tag_ms, None
            else:
                # 直前のサイクルからの差を足して、32 ビットの折り返しを展開する
                step = (cycle.seq - session.last_seq) & _U32_MASK
                seq_n = session.last_seq_n + step
                t_n = session.last_t_n + _ms_after(cycle.t_tag_ms, session.last_t_raw)
                dt_ms = t_n - session.last_t_n if step == 1 else None
            session.last_seq = cycle.seq
            session.last_t_raw = cycle.t_tag_ms
            session.last_seq_n = seq_n
            session.last_t_n = t_n
            live_cycle = LiveCycle(cycle, t_n, seq_n, dt_ms)
            session.cycles.append(live_cycle)
            session.pending.append(live_cycle)
            self.stats.published_cycles += 1
        self._trim(session)
        session.last_seen = now

        if packet.last:
            self._end(session, END_TAG_LAST)

    def note_hello(self, tag_id: int, boot_id: int, session_id: int, config_rev: int | None) -> None:
        """`POST /api/v1/hello` で決まったセッション ID と構成リビジョンを覚える。

        既にライブのセッションがあり構成リビジョンが新たにわかった場合は、`session_start` を送り直して
        クライアントにアンカー座標を取り直させる。
        """
        key = (tag_id, boot_id)
        self._hello[key] = (session_id, config_rev)
        self._hello.move_to_end(key)
        while len(self._hello) > HELLO_MEMORY:
            self._hello.popitem(last=False)
        session = self.sessions.get(tag_id)
        if session is None or session.boot_id != boot_id:
            return
        changed = session.session_id != session_id
        session.session_id = session_id
        if config_rev is not None and config_rev != session.config_rev:
            session.config_rev = config_rev
            changed = True
            if session.active:
                self._broadcast(tag_id, self._session_start_frame(session))
                return
        if changed and not session.active:
            self._broadcast(tag_id, self._session_info_frame(session))

    def note_sessions(self, sessions: Mapping[tuple[int, int], int]) -> None:
        """保存系がコミットしたセッション ID を覚える。

        稼働中のセッションなら以降の append に `session_id` が載る。hello の無い短いセッションが
        コミットより先に終わっていた場合は、以降 append が無いので `session_info` で知らせる。
        """
        for (tag_id, boot_id), session_id in sessions.items():
            session = self.sessions.get(tag_id)
            if session is not None and session.boot_id == boot_id and session.session_id is None:
                session.session_id = session_id
                if not session.active:
                    self._broadcast(tag_id, self._session_info_frame(session))

    # ------------------------------------------------------------ 定期処理

    def tick(self) -> None:
        """溜まった差分を append として配り、途絶えたセッションを終了させる。"""
        now = self._clock()
        for session in self.sessions.values():
            self._flush(session)
            if session.active and now - session.last_seen >= self._session_timeout_s:
                self._end(session, END_TIMEOUT)

    async def run(self) -> None:
        """`tick()` を一定間隔で呼び続ける。止めるときはタスクを cancel する。"""
        stats_task = asyncio.create_task(self._log_stats_periodically(), name="live-stats")
        try:
            while True:
                await asyncio.sleep(self._flush_interval_s)
                try:
                    self.tick()
                except Exception:
                    logger.exception("ライブ配信の定期処理で例外が発生しました")
        finally:
            stats_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stats_task

    def log_stats(self) -> None:
        s = self.stats
        logger.info(
            "LIVE_STATS,active_sessions=%d,subscribers=%d,published_cycles=%d,late_cycles=%d,"
            "stale_cycles=%d,overflow_cycles=%d,dropped_frames=%d,overflow_disconnects=%d",
            self._active_session_count(),
            len(self._subscribers),
            s.published_cycles,
            s.late_cycles,
            s.stale_cycles,
            s.overflow_cycles,
            s.dropped_frames,
            s.overflow_disconnects,
        )

    def _active_session_count(self) -> int:
        return sum(1 for session in self.sessions.values() if session.active)

    async def _log_stats_periodically(self) -> None:
        previous: tuple[object, ...] | None = None
        while True:
            await asyncio.sleep(self._stats_interval_s)
            # log_stats() が出す値をすべて比べる。
            # timeout で稼働中のセッションが 0 になっただけの変化も記録する
            current = (*astuple(self.stats), len(self._subscribers), self._active_session_count())
            if current != previous:
                self.log_stats()
                previous = current

    # ------------------------------------------------------------ 内部処理

    def _start(self, tag_id: int, boot_id: int, now: float) -> LiveSession:
        session_id, config_rev = self._hello.get((tag_id, boot_id), (None, None))
        session = LiveSession(
            tag_id=tag_id, boot_id=boot_id, session_id=session_id, config_rev=config_rev, last_seen=now
        )
        self.sessions[tag_id] = session
        self._broadcast(tag_id, self._session_start_frame(session))
        return session

    def _end(self, session: LiveSession, reason: str) -> None:
        """セッションを終える。既に終了通知を送ってあるセッションには再び送らない。"""
        self._flush(session)
        if reason != END_TIMEOUT:
            # 同じ起動のパケットが後から来ても新しいセッションとは扱わない。
            # timeout だけは同じ起動のまま戻ってくることがあるので覚えない
            self._ended_boots.setdefault(session.tag_id, deque(maxlen=ENDED_BOOTS_PER_TAG)).append(
                session.boot_id
            )
        if session.active:
            session.active = False
            self._broadcast(session.tag_id, {"type": "session_end", **session.identity(), "reason": reason})

    def _flush(self, session: LiveSession) -> None:
        if not session.pending:
            return
        fix, ranges = columns_of(session.pending)
        session.pending = []
        self._broadcast(
            session.tag_id,
            {"type": "append", **session.identity(), "fix": fix.to_json(), "ranges": ranges.to_json()},
        )

    def _trim(self, session: LiveSession) -> None:
        cycles = session.cycles
        while len(cycles) > self._max_buffer_cycles:
            cycles.popleft()
            self.stats.overflow_cycles += 1
        if cycles:
            latest = cycles[-1].t_ms
            while len(cycles) > 1 and latest - cycles[0].t_ms > self._retention_ms:
                cycles.popleft()

    def _session_info_frame(self, session: LiveSession) -> Frame:
        return {"type": "session_info", **session.identity(), "config_rev": session.config_rev}

    def _session_start_frame(self, session: LiveSession) -> Frame:
        return {"type": "session_start", **session.identity(), "config_rev": session.config_rev}

    def _broadcast(self, tag_id: int, frame: Frame) -> None:
        for subscriber in self._subscribers:
            if subscriber.tag_id == tag_id:
                subscriber.push(frame)
