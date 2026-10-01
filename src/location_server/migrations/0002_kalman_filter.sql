-- タグ側のカルマンフィルタの出力を測位記録へ足す。パケット形式 version 2 (doc/server-design.md の 6.2) に対応する。
-- 最小二乗の列 (ok / x_mm / ...) とは独立に持つ。最小二乗が解けず予測だけで進んだサイクルも、
-- フィルタの位置が有効なら kf_x_mm などに値が入る。
-- このマイグレーションより前に書いた行は、フィルタ無効 (kf_ok = 0) で取り込み数と棄却数が NULL になる。

-- フィルタの位置が有効か
ALTER TABLE position_fix ADD COLUMN kf_ok INTEGER NOT NULL DEFAULT 0;
-- このサイクルで観測による更新をしたか。kf_ok = 0 なら 0
ALTER TABLE position_fix ADD COLUMN kf_updated INTEGER NOT NULL DEFAULT 0;
-- このサイクルで最小二乗の解から初期化したか。kf_ok = 0 なら 0
ALTER TABLE position_fix ADD COLUMN kf_init INTEGER NOT NULL DEFAULT 0;
-- フィルタの位置 [mm]。kf_ok = 0 なら NULL
ALTER TABLE position_fix ADD COLUMN kf_x_mm INTEGER;
ALTER TABLE position_fix ADD COLUMN kf_y_mm INTEGER;
ALTER TABLE position_fix ADD COLUMN kf_z_mm INTEGER;
-- 位置の標準偏差 sqrt(Pxx + Pyy) [mm]。65535 で飽和する。kf_ok = 0 なら NULL
ALTER TABLE position_fix ADD COLUMN kf_sigma_mm INTEGER;
-- フィルタが取り込んだ測距と、ゲートで棄却した測距の本数。version 2 の行では常に入れる
ALTER TABLE position_fix ADD COLUMN kf_used INTEGER;
ALTER TABLE position_fix ADD COLUMN kf_rejected INTEGER;
