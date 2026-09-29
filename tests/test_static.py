"""可視化ページの配信のテスト。ページと同梱ファイルが配られ、外部ネットワークを参照しないことを確かめる。"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from location_server.api.app import STATIC_DIR

# 自前で書いたファイル。同梱ライブラリ (vendor/) はコメントに配布元の URL を含むので対象外
OWN_FILES = [
    path
    for path in STATIC_DIR.rglob("*")
    if path.is_file() and "vendor" not in path.relative_to(STATIC_DIR).parts
]


def test_index_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "UWB 測位ビューア" in response.text


def test_assets_referenced_by_page_exist(client: TestClient) -> None:
    html = client.get("/").text
    references = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert "/static/vendor/uplot/uPlot.iife.min.js" in references
    for ref in references:
        assert client.get(ref).status_code == 200, ref


def test_modules_import_only_local_files(client: TestClient) -> None:
    for path in STATIC_DIR.joinpath("js").glob("*.js"):
        for target in re.findall(r'from "([^"]+)"', path.read_text(encoding="utf-8")):
            assert target.startswith("./"), f"{path.name}: {target}"
            assert client.get(f"/static/js/{target[2:]}").status_code == 200, target


def test_own_files_do_not_reach_external_network() -> None:
    assert OWN_FILES
    for path in OWN_FILES:
        assert not re.search(r"https?://", path.read_text(encoding="utf-8")), path.name


def test_vendored_library_has_license() -> None:
    assert (STATIC_DIR / "vendor" / "uplot" / "LICENSE").is_file()
