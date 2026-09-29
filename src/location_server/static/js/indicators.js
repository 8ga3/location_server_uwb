// ライブ表示の指標 (設計文書 8.3)。直近 1 秒の測位レートと欠測率、アンカーごとの成功率、
// 測位失敗の連続回数を、手元に溜めたデータから計算する。時間の基準はタグの millis() で、
// 最後に受けたサイクルから遡って 1 秒を見る。

import { lastIndexAtOrBefore } from "./model.js";

export const WINDOW_MS = 1000;

// 成功率からランプの色を決める。緑 90% 以上 / 黄 50% 以上 / 赤それ未満
export function lampClass(rate) {
  if (rate === null || rate === undefined) return "";
  if (rate >= 0.9) return "ok";
  if (rate >= 0.5) return "warn";
  return "bad";
}

export function liveIndicators(data, windowMs = WINDOW_MS) {
  const fix = data.fix;
  const n = fix.t.length;
  if (n === 0) return null;
  const tEnd = fix.t[n - 1];
  const tStart = tEnd - windowMs;
  const first = lastIndexAtOrBefore(fix.t, tStart) + 1;
  const cycles = n - first;
  let okCycles = 0;
  for (let i = first; i < n; i++) if (fix.ok[i]) okCycles++;
  // seq の欠番は UDP で落ちたサイクル。測位の失敗 (ok = false) とは別に数える
  const expected = cycles > 0 ? fix.seq[n - 1] - fix.seq[first] + 1 : 0;
  const missing = Math.max(0, expected - cycles);
  let consecutiveFailures = 0;
  for (let i = n - 1; i >= 0 && !fix.ok[i]; i--) consecutiveFailures++;

  const anchors = new Map();
  for (const [id, columns] of data.ranges) {
    const from = lastIndexAtOrBefore(columns.t, tStart) + 1;
    let total = 0;
    let ok = 0;
    for (let i = from; i < columns.t.length; i++) {
      total++;
      if (columns.st[i] === 0) ok++;
    }
    anchors.set(id, { total, ok, rate: total > 0 ? ok / total : null });
  }

  return {
    tEnd,
    fixRateHz: okCycles / (windowMs / 1000),
    cycleRateHz: cycles / (windowMs / 1000),
    lossRate: expected > 0 ? missing / expected : null,
    missing,
    consecutiveFailures,
    anchors,
  };
}
