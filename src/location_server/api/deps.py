"""エンドポイントが共有する依存関数。"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from location_server.live import LiveHub
from location_server.models import ConfigSnapshot
from location_server.store import ConfigStore, QueryStore, TelemetryStore
from location_server.units import check_anchor_id, parse_hex_id


def get_store(request: Request) -> ConfigStore:
    """アプリの寿命に紐づく構成ストアを返す。"""
    store: ConfigStore = request.app.state.store
    return store


def get_telemetry_store(request: Request) -> TelemetryStore:
    """アプリの寿命に紐づくテレメトリストアを返す。"""
    store: TelemetryStore = request.app.state.telemetry_store
    return store


def get_query_store(request: Request) -> QueryStore:
    """アプリの寿命に紐づく参照用ストアを返す。"""
    store: QueryStore = request.app.state.query_store
    return store


def get_live_hub(request: Request) -> LiveHub:
    """アプリの寿命に紐づくライブ配信のハブを返す。"""
    hub: LiveHub = request.app.state.live
    return hub


def parse_anchor_id_param(raw: str) -> int:
    """パスやクエリで受けたアンカー ID を解釈する。`0x0100` 形式と 10 進数表記を受け付ける。

    `ValueRangeError` だけでなく `ValueError` 全般を 400 に落とす。ID の解釈は
    `parse_hex_id` 側で完結しているが、未処理例外が 500 として漏れないよう二重に受ける。
    """
    try:
        return check_anchor_id(parse_hex_id(raw))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def snapshot_for_session(store: ConfigStore, config_rev: int | None) -> ConfigSnapshot:
    """セッションが使った構成リビジョンの座標表を返す。

    リビジョンがわからない (hello が届いていない) か、この DB に無いリビジョンの場合は現在の構成を返す。
    どちらを返したかは `ConfigSnapshot.meta.rev` で区別できる。
    """
    if config_rev is not None:
        try:
            return store.snapshot_at(config_rev)
        except LookupError:
            pass
    return store.current_snapshot()
