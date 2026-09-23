"""FastAPI アプリケーションの組み立て。

構成配信 (config) / テレメトリ収集 (ingest) / 参照・可視化 (query) のうち、
フェーズ A では構成配信だけを載せる。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from location_server import __version__
from location_server.api import routes_anchors, routes_config
from location_server.db import connect, migrate
from location_server.settings import Settings, load_settings
from location_server.store import ConfigStore

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """アプリケーションを生成する。

    SQLite への接続は 1 本だけ開き、アプリの寿命と揃える。
    """
    resolved = settings if settings is not None else load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ファームウェア側が起動時にシリアルへ FW_VERSION を出すのと同じ意図で、
        # 起動したサーバーのバージョンをログの先頭に残す
        logger.info("SERVER_VERSION,version=%s", __version__)
        conn = connect(resolved.db_path)
        version = migrate(conn)
        app.state.settings = resolved
        app.state.store = ConfigStore(conn)
        logger.info("DB を開きました: %s (schema %d)", resolved.db_path, version)
        try:
            yield
        finally:
            conn.close()

    app = FastAPI(
        title="UWB 測位デバッグサーバー",
        version=__version__,
        summary="アンカー構成の配信とテレメトリの収集を行うデバッグ用サーバー",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.include_router(routes_config.router)
    app.include_router(routes_anchors.router)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
