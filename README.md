# location_server_uwb

UWB 測位のデバッグ・精度評価に使うサーバーである。アンカーの ID と設置座標を保持してタグへ配信し、
タグから送られてくる測距結果と自己位置推定結果を蓄積して可視化する。

設計の正本は [doc/server-design.md](doc/server-design.md) である。実装と文書が食い違う変更は行わない。

## 実装状況

フェーズ A (構成配信) までを実装している。

| フェーズ | 内容 | 状態 |
| --- | --- | --- |
| A | SQLite スキーマ、構成配信 API、アンカー管理 API、座標入力 CLI | 実装済み |
| B | UDP によるテレメトリ収集 | 未着手 |
| C | ライブ配信と可視化ページ | 未着手 |
| D | self-survey 連携 | 未着手 |

フェーズ A のうち「タグ側の Wi-Fi 取得 + NVS キャッシュ」はファームウェア側の作業であり、
このリポジトリには含まれない。

## 必要なもの

- Python 3.14
- [uv](https://docs.astral.sh/uv/)

## セットアップ

```sh
uv sync
```

## 起動

```sh
uv run python -m location_server --db data/2026-09-21-run1.db --port 8000
```

設定は環境変数でも与えられる。

| 環境変数 | 既定値 | 内容 |
| --- | --- | --- |
| `UWB_DB_PATH` | `data/location.db` | SQLite ファイルのパス。実験ごとに分ける |
| `UWB_HOST` | `0.0.0.0` | 待ち受けアドレス |
| `UWB_PORT` | `8000` | 待ち受けポート |
| `UWB_AUTH_TOKEN` | (未設定) | 設定すると書き込み系 API に `X-Auth-Token` ヘッダを要求する |

コマンドライン引数を与えた場合は環境変数より優先される。

## エンドポイント

| メソッド | パス | 用途 |
| --- | --- | --- |
| `GET` | `/api/v1/config` | タグへ配る構成一式。`ETag` / `If-None-Match` による `304` に対応する |
| `GET` | `/api/v1/config/revisions/{rev}` | 過去のリビジョン時点の構成 |
| `PUT` | `/api/v1/config/telemetry` | テレメトリ送信先と `batch_cycles` の更新 |
| `GET` | `/api/v1/anchors` | アンカー一覧。無効化されているものも含む |
| `PUT` | `/api/v1/anchors/{id}` | アンカー 1 台の登録・更新 |
| `GET` | `/healthz` | 死活確認 |

座標は API の JSON でのみメートル表記とし、通信と DB 内部では整数ミリメートルで保持する。
アンカー ID は UWB の responder address そのもので、JSON では `0x0100` 形式の文字列で表す。

構成を書き換えると `config_meta` に新しい `rev` が積まれ、その時点の全アンカーが `config_anchor` へ
記録される。走行後に `GET /api/v1/config/revisions/{rev}` を引けば、当時の座標表をそのまま復元できる。

## 座標の入力

`tools/anchor_cli.py` はサーバーの管理 API を叩く CLI である。追加の依存はない。

```sh
# アンカーを登録する (座標はメートル)
uv run python tools/anchor_cli.py set 0x0100 --x 0 --y 0 --z 1.8 --label 北西の柱
uv run python tools/anchor_cli.py set 0x0101 --x 5.12 --y 0 --z 1.8
uv run python tools/anchor_cli.py set 0x0102 --x 5.08 --y 4.31 --z 1.8
uv run python tools/anchor_cli.py set 0x0103 --x 0.03 --y 4.29 --z 1.8

# 一覧を見る
uv run python tools/anchor_cli.py list

# テレメトリ送信先を設定する (port 0 でタグ側の送信を停止する)
uv run python tools/anchor_cli.py telemetry --host 192.168.1.10 --port 47100 --batch-cycles 4

# タグへ配られる構成を確認する
uv run python tools/anchor_cli.py config
```

接続先は `--server` または環境変数 `UWB_SERVER_URL` で変えられる。
`UWB_AUTH_TOKEN` を設定してサーバーを起動している場合は、CLI 側にも同じ環境変数か `--token` を与える。

## 開発

```sh
uv run pytest          # テスト
uv run ruff check .    # lint
uv run ruff format .   # 整形
uv run mypy src tools tests   # 型チェック
npx markdownlint-cli2  # Markdown の lint
```
