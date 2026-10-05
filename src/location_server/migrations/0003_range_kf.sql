-- 測距記録へ、その測距をタグ側のカルマンフィルタがどう扱ったかを足す。
-- パケット形式 version 3 (doc/server-design.md の 6.2) の測距レコードの kf に対応する。
-- 0 = 使っていない、1 = フィルタが取り込んだ、2 = イノベーションのゲートで棄却した。
-- このマイグレーションより前に書いた行は NULL になる。

ALTER TABLE range_sample ADD COLUMN kf INTEGER;
