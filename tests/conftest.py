"""テスト共通のフィクスチャ。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from location_server.api import create_app
from location_server.db import connect, migrate
from location_server.settings import Settings
from location_server.store import ConfigStore


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = connect(":memory:")
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def store(conn: sqlite3.Connection) -> ConfigStore:
    return ConfigStore(conn)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def settings(db_path: Path) -> Settings:
    return Settings(db_path=db_path, host="127.0.0.1", port=0, auth_token=None)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def token_client(db_path: Path) -> Iterator[TestClient]:
    secured = Settings(db_path=db_path, host="127.0.0.1", port=0, auth_token="s3cret")
    with TestClient(create_app(secured)) as test_client:
        yield test_client
