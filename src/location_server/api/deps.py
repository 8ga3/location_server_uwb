"""エンドポイントが共有する依存関数。"""

from __future__ import annotations

from fastapi import Request

from location_server.store import ConfigStore, TelemetryStore


def get_store(request: Request) -> ConfigStore:
    """アプリの寿命に紐づく構成ストアを返す。"""
    store: ConfigStore = request.app.state.store
    return store


def get_telemetry_store(request: Request) -> TelemetryStore:
    """アプリの寿命に紐づくテレメトリストアを返す。"""
    store: TelemetryStore = request.app.state.telemetry_store
    return store
