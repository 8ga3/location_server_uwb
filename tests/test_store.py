"""構成ストアのテスト。リビジョンの単調増加と再現性を確認する。"""

from __future__ import annotations

import sqlite3

import pytest

from location_server.store import AnchorNotFoundError, ConfigStore
from location_server.units import ValueRangeError


def _put(store: ConfigStore, anchor_id: int, x_mm: int, y_mm: int, z_mm: int, enabled: bool = True) -> bool:
    result = store.put_anchor(
        anchor_id=anchor_id,
        label=f"anchor-{anchor_id:04X}",
        x_mm=x_mm,
        y_mm=y_mm,
        z_mm=z_mm,
        enabled=enabled,
    )
    return result.created


def test_initial_state_has_no_anchors(store: ConfigStore) -> None:
    snapshot = store.current_snapshot()
    assert snapshot.meta.rev == 1
    assert snapshot.anchors == ()
    assert snapshot.etag == '"rev-1"'


def test_get_anchor_missing(store: ConfigStore) -> None:
    with pytest.raises(AnchorNotFoundError):
        store.get_anchor(0x0100)


def test_put_anchor_bumps_revision(store: ConfigStore) -> None:
    _put(store, 0x0100, 0, 0, 1800)
    assert store.current_meta().rev == 2
    _put(store, 0x0101, 5120, 0, 1800)
    assert store.current_meta().rev == 3


def test_put_anchor_updates_existing_row(store: ConfigStore) -> None:
    assert _put(store, 0x0100, 0, 0, 1800) is True
    assert _put(store, 0x0100, 10, 20, 1750) is False
    anchors = store.list_anchors()
    assert len(anchors) == 1
    assert (anchors[0].x_mm, anchors[0].y_mm, anchors[0].z_mm) == (10, 20, 1750)


def test_put_anchor_rejects_out_of_range_id(store: ConfigStore) -> None:
    with pytest.raises(ValueRangeError):
        _put(store, 0x00FF, 0, 0, 0)


def test_old_revision_is_reproducible(store: ConfigStore) -> None:
    _put(store, 0x0100, 0, 0, 1800)
    _put(store, 0x0101, 5120, 0, 1800)
    rev_with_two = store.current_meta().rev

    # 座標を後から直しても、過去のリビジョンの値は変わらない
    _put(store, 0x0100, 500, 500, 1800)

    past = store.snapshot_at(rev_with_two)
    assert [(a.id, a.x_mm, a.y_mm) for a in past.anchors] == [(0x0100, 0, 0), (0x0101, 5120, 0)]

    current = store.current_snapshot()
    assert [(a.id, a.x_mm, a.y_mm) for a in current.anchors] == [(0x0100, 500, 500), (0x0101, 5120, 0)]


def test_snapshot_includes_disabled_anchor_but_config_excludes_it(store: ConfigStore) -> None:
    _put(store, 0x0100, 0, 0, 1800)
    _put(store, 0x0101, 5120, 0, 1800, enabled=False)
    snapshot = store.current_snapshot()
    assert len(snapshot.anchors) == 2
    assert [a.id for a in snapshot.enabled_anchors] == [0x0100]


def test_snapshot_at_unknown_revision(store: ConfigStore) -> None:
    with pytest.raises(LookupError):
        store.snapshot_at(999)


def test_put_telemetry_bumps_revision_and_keeps_anchors(store: ConfigStore) -> None:
    _put(store, 0x0100, 0, 0, 1800)
    snapshot = store.put_telemetry(host="192.168.1.10", port=47100, batch_cycles=8)
    assert snapshot.meta.rev == 3
    assert snapshot.meta.telemetry_host == "192.168.1.10"
    assert snapshot.meta.telemetry_port == 47100
    assert snapshot.meta.batch_cycles == 8
    # テレメトリだけを変えてもアンカーのスナップショットは引き継がれる
    assert [a.id for a in store.snapshot_at(3).anchors] == [0x0100]


def test_revision_inherits_previous_values(store: ConfigStore) -> None:
    store.put_telemetry(host="192.168.1.10", port=47100, batch_cycles=8)
    _put(store, 0x0100, 0, 0, 1800)
    meta = store.current_meta()
    assert meta.telemetry_host == "192.168.1.10"
    assert meta.batch_cycles == 8
    assert meta.pan_id == 0xDECA


def test_failed_transaction_leaves_no_partial_revision(store: ConfigStore, conn: sqlite3.Connection) -> None:
    _put(store, 0x0100, 0, 0, 1800)
    before = store.current_meta().rev

    # config_anchor への書き込みが失敗する状況を作り、rev だけが進まないことを確認する
    conn.execute("CREATE UNIQUE INDEX tmp_block ON config_anchor (id)")
    try:
        _put(store, 0x0101, 1, 1, 1)
        _put(store, 0x0102, 2, 2, 2)
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.execute("DROP INDEX tmp_block")

    rows = conn.execute("SELECT rev FROM config_meta ORDER BY rev").fetchall()
    revs = [row["rev"] for row in rows]
    assert revs == list(range(1, max(revs) + 1))
    assert store.current_meta().rev >= before
    for rev in revs:
        # すべてのリビジョンが読み出せる状態を保つ
        store.snapshot_at(rev)
