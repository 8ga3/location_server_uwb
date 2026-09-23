"""アンカー表と構成リビジョンの読み書き。

構成を書き換える操作は必ず `config_meta` に新しい `rev` を追加し、同時にその時点の全アンカーを
`config_anchor` へ写す (設計文書 4.2 / 5.3)。これによって、あるセッションが使った座標表を
後から `session.config_rev` だけで復元できる。
"""

from __future__ import annotations

import sqlite3
import threading
from typing import NamedTuple

from location_server.db import utc_now_text
from location_server.models import ANCHOR_SOURCE_MANUAL, Anchor, ConfigMeta, ConfigSnapshot
from location_server.units import check_anchor_id


class AnchorNotFoundError(LookupError):
    """指定した ID のアンカーが登録されていない場合に送出する。"""


class AnchorUpdate(NamedTuple):
    """アンカー更新の結果。`created` は新規登録だったかを表す。"""

    anchor: Anchor
    snapshot: ConfigSnapshot
    created: bool


class ConfigStore:
    """構成配信に使う表への単一ライターなアクセス経路。

    SQLite は単一ライターで運用するため、書き込みを含む操作はロックで直列化する。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 参照

    def current_meta(self) -> ConfigMeta:
        """最新の構成リビジョンを返す。"""
        with self._lock:
            return self._current_meta()

    def list_anchors(self) -> tuple[Anchor, ...]:
        """現在のアンカー一覧を返す。無効化されているものも含む。"""
        with self._lock:
            return self._list_anchors()

    def get_anchor(self, anchor_id: int) -> Anchor:
        """アンカー 1 台を返す。存在しない場合は `AnchorNotFoundError` を送出する。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM anchor WHERE id = ?", (anchor_id,)).fetchone()
        if row is None:
            raise AnchorNotFoundError(anchor_id)
        return Anchor.from_row(row)

    def current_snapshot(self) -> ConfigSnapshot:
        """最新リビジョンの構成一式を返す。"""
        with self._lock:
            return ConfigSnapshot(meta=self._current_meta(), anchors=self._list_anchors())

    def snapshot_at(self, rev: int) -> ConfigSnapshot:
        """指定したリビジョン時点の構成一式を返す。"""
        with self._lock:
            meta_row = self._conn.execute("SELECT * FROM config_meta WHERE rev = ?", (rev,)).fetchone()
            if meta_row is None:
                raise LookupError(f"構成リビジョンが存在しません: {rev}")
            anchor_rows = self._conn.execute(
                "SELECT id, label, x_mm, y_mm, z_mm, enabled, source FROM config_anchor "
                "WHERE rev = ? ORDER BY id",
                (rev,),
            ).fetchall()
        meta = ConfigMeta.from_row(meta_row)
        anchors = tuple(
            Anchor(
                id=row["id"],
                label=row["label"],
                x_mm=row["x_mm"],
                y_mm=row["y_mm"],
                z_mm=row["z_mm"],
                enabled=bool(row["enabled"]),
                source=row["source"],
                updated_at=meta.created_at,
            )
            for row in anchor_rows
        )
        return ConfigSnapshot(meta=meta, anchors=anchors)

    # ------------------------------------------------------------------ 更新

    def put_anchor(
        self,
        *,
        anchor_id: int,
        label: str | None,
        x_mm: int,
        y_mm: int,
        z_mm: int,
        enabled: bool,
        source: str = ANCHOR_SOURCE_MANUAL,
        note: str | None = None,
    ) -> AnchorUpdate:
        """アンカー 1 台を登録または更新し、更新後のアンカーと新しい構成を返す。

        新規登録か更新かの判定も同じトランザクション内で行い、
        並行した呼び出しで判定と書き込みがずれないようにする。
        """
        check_anchor_id(anchor_id)
        now = utc_now_text()
        change_note = note if note is not None else f"アンカー 0x{anchor_id:04X} を更新"
        with self._lock, self._transaction():
            existing = self._conn.execute("SELECT 1 FROM anchor WHERE id = ?", (anchor_id,)).fetchone()
            created = existing is None
            self._conn.execute(
                """
                INSERT INTO anchor (id, label, x_mm, y_mm, z_mm, enabled, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    label = excluded.label,
                    x_mm = excluded.x_mm,
                    y_mm = excluded.y_mm,
                    z_mm = excluded.z_mm,
                    enabled = excluded.enabled,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (anchor_id, label, x_mm, y_mm, z_mm, int(enabled), source, now),
            )
            meta = self._append_revision(note=change_note, created_at=now)
            anchors = self._list_anchors()
            self._write_snapshot(meta.rev, anchors)
        anchor = next(item for item in anchors if item.id == anchor_id)
        return AnchorUpdate(
            anchor=anchor, snapshot=ConfigSnapshot(meta=meta, anchors=anchors), created=created
        )

    def put_telemetry(
        self,
        *,
        host: str,
        port: int,
        batch_cycles: int,
        note: str | None = None,
    ) -> ConfigSnapshot:
        """テレメトリ送信先と束ねるサイクル数を更新し、新しい構成を返す。"""
        now = utc_now_text()
        change_note = note if note is not None else "テレメトリ設定を更新"
        with self._lock, self._transaction():
            meta = self._append_revision(
                note=change_note,
                created_at=now,
                telemetry_host=host,
                telemetry_port=port,
                batch_cycles=batch_cycles,
            )
            anchors = self._list_anchors()
            self._write_snapshot(meta.rev, anchors)
        return ConfigSnapshot(meta=meta, anchors=anchors)

    # ------------------------------------------------------------- 内部処理

    def _transaction(self) -> _Transaction:
        return _Transaction(self._conn)

    def _current_meta(self) -> ConfigMeta:
        row = self._conn.execute("SELECT * FROM config_meta ORDER BY rev DESC LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("config_meta が空です。マイグレーションが適用されていません")
        return ConfigMeta.from_row(row)

    def _list_anchors(self) -> tuple[Anchor, ...]:
        rows = self._conn.execute("SELECT * FROM anchor ORDER BY id").fetchall()
        return tuple(Anchor.from_row(row) for row in rows)

    def _append_revision(
        self,
        *,
        note: str,
        created_at: str,
        pan_id: int | None = None,
        bias_mm: int | None = None,
        telemetry_host: str | None = None,
        telemetry_port: int | None = None,
        batch_cycles: int | None = None,
    ) -> ConfigMeta:
        """直前のリビジョンを引き継いだ新しい `rev` を 1 つ追加する。"""
        previous = self._current_meta()
        meta = ConfigMeta(
            rev=previous.rev + 1,
            pan_id=previous.pan_id if pan_id is None else pan_id,
            bias_mm=previous.bias_mm if bias_mm is None else bias_mm,
            telemetry_host=previous.telemetry_host if telemetry_host is None else telemetry_host,
            telemetry_port=previous.telemetry_port if telemetry_port is None else telemetry_port,
            batch_cycles=previous.batch_cycles if batch_cycles is None else batch_cycles,
            note=note,
            created_at=created_at,
        )
        self._conn.execute(
            """
            INSERT INTO config_meta
                (rev, pan_id, bias_mm, telemetry_host, telemetry_port, batch_cycles, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meta.rev,
                meta.pan_id,
                meta.bias_mm,
                meta.telemetry_host,
                meta.telemetry_port,
                meta.batch_cycles,
                meta.note,
                meta.created_at,
            ),
        )
        return meta

    def _write_snapshot(self, rev: int, anchors: tuple[Anchor, ...]) -> None:
        self._conn.executemany(
            """
            INSERT INTO config_anchor (rev, id, label, x_mm, y_mm, z_mm, enabled, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(rev, a.id, a.label, a.x_mm, a.y_mm, a.z_mm, int(a.enabled), a.source) for a in anchors],
        )


class _Transaction:
    """`BEGIN IMMEDIATE` から `COMMIT` / `ROLLBACK` までを囲むコンテキスト。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> _Transaction:
        self._conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")
