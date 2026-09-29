"""FastAPI アプリケーションの組み立て。

構成配信 (config) / テレメトリ収集 (ingest) / 参照・可視化 (query/viz) の 3 層を 1 プロセスに載せる。
可視化ページは `/` で配る静的ページである。ライブラリも同梱してあり、外部ネットワークへは接続しない
(設計文書 8 節)。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from location_server import __version__
from location_server.api import routes_anchors, routes_config, routes_live, routes_sessions
from location_server.db import connect, migrate
from location_server.ingest.service import IngestService
from location_server.live import LiveHub
from location_server.settings import Settings, load_settings
from location_server.store import ConfigStore, QueryStore, TelemetryStore

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    """アプリケーションを生成する。

    SQLite への接続は 1 本だけ開き、アプリの寿命と揃える。構成配信とテレメトリ収集は
    同じ接続と同じロックを共有し、単一ライターを保つ。
    """
    resolved = settings if settings is not None else load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ファームウェア側が起動時にシリアルへ FW_VERSION を出すのと同じ意図で、
        # 起動したサーバーのバージョンをログの先頭に残す
        logger.info("SERVER_VERSION,version=%s", __version__)
        conn = connect(resolved.db_path)
        version = migrate(conn)
        lock = threading.Lock()
        config_store = ConfigStore(conn, lock)
        telemetry_store = TelemetryStore(conn, lock)
        live = LiveHub()
        app.state.settings = resolved
        app.state.store = config_store
        app.state.telemetry_store = telemetry_store
        app.state.query_store = QueryStore(conn, lock)
        app.state.live = live
        app.state.ingest = None
        logger.info("DB を開きました: %s (schema %d)", resolved.db_path, version)
        ingest: IngestService | None = None
        live_task = asyncio.create_task(live.run(), name="live-hub")
        try:
            if resolved.udp_port is not None:
                ingest = IngestService(telemetry_store, live=live)
                await ingest.start(resolved.host, resolved.udp_port)
                app.state.ingest = ingest
                _warn_if_telemetry_port_differs(config_store, ingest.local_port)
            else:
                logger.info("UDP 受信は無効です (テレメトリを収集しません)")
            yield
        finally:
            if ingest is not None:
                await ingest.stop()
            live_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await live_task
            conn.close()

    app = FastAPI(
        title="UWB 測位デバッグサーバー",
        version=__version__,
        summary="アンカー構成の配信、テレメトリの収集と可視化を行うデバッグ用サーバー",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.include_router(routes_config.router)
    app.include_router(routes_anchors.router)
    app.include_router(routes_sessions.router)
    app.include_router(routes_live.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """可視化ページ (設計文書 8 節)。ライブと再生を同じページで切り替える。"""
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def _warn_if_telemetry_port_differs(store: ConfigStore, listening: int | None) -> None:
    """タグへ配っている送信先ポートと受信ポートが食い違っていれば警告する。

    送信先は構成 (`config_meta`) が正本で、受信ポートは起動設定なので、両者は自動では揃わない。
    `telemetry_port = 0` はタグ側の送信停止を表すので、そのときは何も言わない。
    """
    meta = store.current_meta()
    if meta.telemetry_port != 0 and meta.telemetry_port != listening:
        logger.warning(
            "タグへ配っているテレメトリ送信先のポート (%d, rev %d) と UDP の受信ポート (%s) が異なります",
            meta.telemetry_port,
            meta.rev,
            listening,
        )
