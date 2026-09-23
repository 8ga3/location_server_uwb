-- 初期スキーマ。doc/server-design.md の 4.1 に対応する。
-- 座標は通信と DB 内部では整数ミリメートルで保持し、API の JSON でのみメートルへ変換する。

-- アンカーの ID と設置座標。id はそのまま UWB の responderAddress (0x0100..0xFFFE)
CREATE TABLE anchor (
    id          INTEGER PRIMARY KEY,
    label       TEXT,
    x_mm        INTEGER NOT NULL,
    y_mm        INTEGER NOT NULL,
    z_mm        INTEGER NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    source      TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- 構成全体のリビジョン。タグはこの値でキャッシュの有効性を判断する
CREATE TABLE config_meta (
    rev              INTEGER PRIMARY KEY,
    pan_id           INTEGER NOT NULL,
    bias_mm          INTEGER NOT NULL,
    telemetry_host   TEXT NOT NULL,
    telemetry_port   INTEGER NOT NULL,
    batch_cycles     INTEGER NOT NULL,
    note             TEXT,
    created_at       TEXT NOT NULL
);

-- リビジョンごとのアンカー座標スナップショット。過去のセッションが使った座標表を復元する
CREATE TABLE config_anchor (
    rev         INTEGER NOT NULL REFERENCES config_meta(rev),
    id          INTEGER NOT NULL,
    label       TEXT,
    x_mm        INTEGER NOT NULL,
    y_mm        INTEGER NOT NULL,
    z_mm        INTEGER NOT NULL,
    enabled     INTEGER NOT NULL,
    source      TEXT NOT NULL,
    PRIMARY KEY (rev, id)
) WITHOUT ROWID;

-- タグの 1 回の起動 = 1 セッション。millis() の基準がここで切れる
CREATE TABLE session (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_id        INTEGER NOT NULL,
    boot_id       INTEGER NOT NULL,
    config_rev    INTEGER,
    fw_version    TEXT,
    started_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    note          TEXT,
    UNIQUE (tag_id, boot_id)
);

-- 測距 1 本ぶん。1 サイクルにつきアンカー台数だけ行が入る
CREATE TABLE range_sample (
    session_id   INTEGER NOT NULL REFERENCES session(id),
    seq          INTEGER NOT NULL,
    t_tag_ms     INTEGER NOT NULL,
    anchor_id    INTEGER NOT NULL,
    status       INTEGER NOT NULL,
    distance_mm  INTEGER,
    elapsed_ms   INTEGER,
    PRIMARY KEY (session_id, seq, anchor_id)
) WITHOUT ROWID;

-- そのサイクルでタグが出した自己位置
CREATE TABLE position_fix (
    session_id    INTEGER NOT NULL REFERENCES session(id),
    seq           INTEGER NOT NULL,
    t_tag_ms      INTEGER NOT NULL,
    recv_at       TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    x_mm          INTEGER,
    y_mm          INTEGER,
    z_mm          INTEGER,
    used_count    INTEGER,
    residual_mm   INTEGER,
    method        TEXT,
    PRIMARY KEY (session_id, seq)
) WITHOUT ROWID;

CREATE INDEX idx_range_anchor ON range_sample (session_id, anchor_id, seq);
CREATE INDEX idx_fix_time     ON position_fix (session_id, t_tag_ms);
