"""エンドポイントが共有する依存関数。"""

from __future__ import annotations

from fastapi import Request

from location_server.store import ConfigStore


def get_store(request: Request) -> ConfigStore:
    """アプリの寿命に紐づく構成ストアを返す。"""
    store: ConfigStore = request.app.state.store
    return store
