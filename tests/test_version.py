"""バージョン管理のテスト。

単一の情報源である `pyproject.toml` の値と、実行時に見える値・README の記載が
食い違わないことを確認する。運用方針は AGENTS.md の「バージョン管理」を参照。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

import location_server
from location_server.version import DISTRIBUTION_NAME, get_version

REPO_ROOT = Path(__file__).resolve().parent.parent

# SemVer の MAJOR.MINOR.PATCH。開発中は -dev サフィックスを付ける
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(-dev)?")


def _pyproject_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version: str = data["project"]["version"]
    return version


def test_pyproject_version_follows_semver() -> None:
    assert VERSION_PATTERN.fullmatch(_pyproject_version())


def test_package_version_matches_pyproject() -> None:
    assert location_server.__version__ == _pyproject_version()
    assert get_version() == _pyproject_version()


def test_distribution_name_matches_pyproject() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == DISTRIBUTION_NAME


def test_readme_version_matches_pyproject() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"^バージョン: `([^`]+)`$", readme, flags=re.MULTILINE)
    assert match is not None, "README.md にバージョンの記載が見つかりません"
    assert match.group(1) == _pyproject_version()


def test_healthz_reports_version(client: TestClient) -> None:
    payload = client.get("/healthz").json()
    assert payload["version"] == _pyproject_version()


def test_license_is_mit() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["license"] == "MIT"
    license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License")
