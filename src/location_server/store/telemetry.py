"""セッションとテレメトリ (測距・測位) の書き込み。

タグの 1 回の起動を `(tag_id, boot_id)` で識別する 1 セッションとして扱う (設計文書 4.2)。
セッション行は `POST /api/v1/hello` か、UDP パケットを最初に受けた時点のどちらか早いほうで作る。
後から届いたほうは既存の行へ足りない情報を書き足すだけで、`started_at` は動かさない。

セッション行の作成と更新には `INSERT ... ON CONFLICT DO UPDATE` を使わず、`UPDATE` を先に試して
該当が無いときだけ `INSERT` する。`session.id` は `AUTOINCREMENT` なので、UPSERT が既存行の更新に
落ちた場合も番号が 1 つ消費され、パケットを受けるたびに ID が飛んでしまうためである。

受信したパケットはまとめて 1 トランザクションで書く (設計文書 6.3)。同じ `(session_id, seq)` を
もう一度受けた場合は先に書いた行を残し、後から届いたものは捨てる。UDP の重複配送や、
タグ側の再送ではない偶然の重複で行が書き換わらないようにするためである。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from location_server.db import Transaction
from location_server.ingest.packet import CycleRecord, TelemetryPacket

METHOD_TRILAT2D = "trilat2d"
METHOD_TRILAT3D = "trilat3d"


@dataclass(frozen=True, slots=True)
class ReceivedPacket:
    """受信時刻を添えたパケット。`recv_at` はサーバーの壁時計 (ISO 8601)。"""

    packet: TelemetryPacket
    recv_at: str


@dataclass(frozen=True, slots=True)
class WriteResult:
    """1 回の一括書き込みの結果。`rows` は実際に挿入された行数 (重複で捨てた行は含まない)。"""

    packets: int
    rows: int
    duplicate_rows: int
    sessions: dict[tuple[int, int], int]


class TelemetryStore:
    """セッション表とテレメトリ表への単一ライターなアクセス経路。

    構成ストアと同じ接続・同じロックを共有して使う。
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock) -> None:
        self._conn = conn
        self._lock = lock

    def hello(
        self,
        *,
        tag_id: int,
        boot_id: int,
        fw_version: str | None,
        config_rev: int | None,
        now: str,
    ) -> int:
        """セッション開始通知を記録し、セッション ID を返す。

        UDP の受信で先にセッションが作られていた場合も、`fw_version` と `config_rev` を書き足す。
        本文で省略された (`None` の) 項目は、既に記録されている値を消さずに残す。
        """
        with self._lock, Transaction(self._conn):
            row = self._conn.execute(
                """
                UPDATE session SET
                    config_rev = coalesce(?, config_rev),
                    fw_version = coalesce(?, fw_version),
                    last_seen_at = max(last_seen_at, ?)
                WHERE tag_id = ? AND boot_id = ?
                RETURNING id
                """,
                (config_rev, fw_version, now, tag_id, boot_id),
            ).fetchone()
            if row is None:
                row = self._conn.execute(
                    """
                    INSERT INTO session (tag_id, boot_id, config_rev, fw_version, started_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    (tag_id, boot_id, config_rev, fw_version, now, now),
                ).fetchone()
        session_id: int = row[0]
        return session_id

    def write_packets(self, received: Sequence[ReceivedPacket]) -> WriteResult:
        """パケット群を 1 トランザクションで書き込む。

        途中で失敗した場合はトランザクション全体を巻き戻して例外をそのまま送出する。
        呼び出し側 (`TelemetryWriter`) がその束を破棄して数える。
        """
        rows = 0
        attempted = 0
        # セッション行は束の中でセッションごとに 1 回だけ触る。最初と最後の受信時刻を先に集めておく
        seen: dict[tuple[int, int], tuple[str, str]] = {}
        for item in received:
            key = (item.packet.tag_id, item.packet.boot_id)
            first, last = seen.get(key, (item.recv_at, item.recv_at))
            seen[key] = (min(first, item.recv_at), max(last, item.recv_at))
        sessions: dict[tuple[int, int], int] = {}
        with self._lock, Transaction(self._conn):
            for key, (first, last) in seen.items():
                sessions[key] = self._touch_session(key, first, last)
            for item in received:
                packet = item.packet
                session_id = sessions[(packet.tag_id, packet.boot_id)]
                for cycle in packet.cycles:
                    attempted += 1 + len(cycle.ranges)
                    rows += self._insert_cycle(session_id, cycle, item.recv_at)
        return WriteResult(
            packets=len(received), rows=rows, duplicate_rows=attempted - rows, sessions=sessions
        )

    # ------------------------------------------------------------- 内部処理

    def _touch_session(self, key: tuple[int, int], first_seen: str, last_seen: str) -> int:
        """セッションが無ければ作り、あれば受信時刻の範囲を広げて ID を返す。

        受信の順序は保証されないので、`started_at` は小さいほう、`last_seen_at` は大きいほうを残す。
        UDP をキューへ積んでから書き込むまでの間に hello が先に行を作った場合も、
        `started_at` は UDP を最初に受けた時刻まで戻る。時刻はどちらも `utc_now_text()` の
        同じ書式なので、文字列の大小が時刻の前後と一致する。
        """
        tag_id, boot_id = key
        row = self._conn.execute(
            """
            UPDATE session SET
                started_at = min(started_at, ?),
                last_seen_at = max(last_seen_at, ?)
            WHERE tag_id = ? AND boot_id = ?
            RETURNING id
            """,
            (first_seen, last_seen, tag_id, boot_id),
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                """
                INSERT INTO session (tag_id, boot_id, started_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                RETURNING id
                """,
                (tag_id, boot_id, first_seen, last_seen),
            ).fetchone()
        session_id: int = row[0]
        return session_id

    def _insert_cycle(self, session_id: int, cycle: CycleRecord, recv_at: str) -> int:
        """1 サイクルぶんの測位 1 行と測距 N 行を挿入し、挿入できた行数を返す。

        同じ `(session_id, seq)` の測位行が既にあれば、そのサイクルは測距行も含めて丸ごと捨てる。
        先に届いたサイクルへ、後から届いたパケットの測距行が混ざらないようにするためである。

        測位に失敗したサイクルは座標と方式を NULL にする。タグは失敗時の座標欄に意味のある値を
        入れないためで、成功したサイクルの値は発散していてもそのまま残す。
        測距も同様に、`status != 0` のときは距離を NULL にする (設計文書 4.1)。
        """
        ok = cycle.fix_ok
        method = (METHOD_TRILAT3D if cycle.fix_3d else METHOD_TRILAT2D) if ok else None
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO position_fix
                (session_id, seq, t_tag_ms, recv_at, ok, x_mm, y_mm, z_mm, used_count, residual_mm, method)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                cycle.seq,
                cycle.t_tag_ms,
                recv_at,
                int(ok),
                cycle.x_mm if ok else None,
                cycle.y_mm if ok else None,
                cycle.z_mm if ok else None,
                cycle.used_count,
                cycle.residual_mm,
                method,
            ),
        )
        if cursor.rowcount == 0:
            return 0
        inserted = cursor.rowcount
        cursor = self._conn.executemany(
            """
            INSERT OR IGNORE INTO range_sample
                (session_id, seq, t_tag_ms, anchor_id, status, distance_mm, elapsed_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    session_id,
                    cycle.seq,
                    cycle.t_tag_ms,
                    r.anchor_id,
                    r.status,
                    r.distance_mm if r.ok else None,
                    r.elapsed_ms,
                )
                for r in cycle.ranges
            ],
        )
        return inserted + cursor.rowcount
