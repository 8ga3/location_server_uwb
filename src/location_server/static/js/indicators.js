// ライブ表示の指標 (設計文書 8.3)。直近 1 秒の測位レートと欠測率、アンカーごとの成功率、
// 測位失敗の連続回数、フィルタの集計を、手元に溜めたデータから計算する。時間の基準はタグの millis() で、
// 最後に受けたサイクルから遡って 1 秒を見る。フィルタの集計は再生のサマリ (サーバーが返す kf_*) と
// 同じ数え方で、範囲だけを直近 1 秒に絞る。

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

  // フィルタ: 有効なサイクル、そのうち予測だけのサイクル、初期化、棄却した測距、σ の平均
  let kfOk = 0;
  let kfPredicted = 0;
  let kfInit = 0;
  let kfRejected = 0;
  let sigmaSum = 0;
  let sigmaCount = 0;
  for (let i = first; i < n; i++) {
    kfRejected += fix.krej[i] ?? 0;
    if (!fix.kok[i]) continue;
    kfOk++;
    if (!fix.kupd[i]) kfPredicted++;
    if (fix.kinit[i]) kfInit++;
    if (fix.ksig[i] !== null && fix.ksig[i] !== undefined) {
      sigmaSum += fix.ksig[i];
      sigmaCount++;
    }
  }

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
    kfRate: cycles > 0 ? kfOk / cycles : null,
    kfPredicted,
    kfInit,
    kfRejected,
    kfSigmaMean: sigmaCount > 0 ? sigmaSum / sigmaCount : null,
    anchors,
  };
}
