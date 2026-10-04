// フィルタの効果の指標 (設計文書 8.4)。最小二乗の解とフィルタ後の位置を、どちらも有効なサイクルどうしで比べる。
// tools/filter_compare.py も同じ定義で計算するので、定義を変えるときは両方を揃える。

// 跳びとして数えるしきい値 [m]
export const JUMP_THRESHOLD_M = 0.05;

function positionStats(xs, ys, steps) {
  const n = xs.length;
  if (n === 0) return null;
  let mx = 0;
  let my = 0;
  for (let i = 0; i < n; i++) {
    mx += xs[i];
    my += ys[i];
  }
  mx /= n;
  my /= n;
  let sq = 0;
  for (let i = 0; i < n; i++) sq += (xs[i] - mx) ** 2 + (ys[i] - my) ** 2;
  let stepSq = 0;
  let stepMax = null;
  let jumps = 0;
  for (const s of steps) {
    stepSq += s * s;
    if (stepMax === null || s > stepMax) stepMax = s;
    if (s > JUMP_THRESHOLD_M) jumps++;
  }
  return {
    meanX: mx,
    meanY: my,
    scatterRms: Math.sqrt(sq / n),
    stepRms: steps.length > 0 ? Math.sqrt(stepSq / steps.length) : null,
    stepMax,
    jumps: steps.length > 0 ? jumps : null,
  };
}

// fix の from..to (両端を含む) の範囲で指標を求める。対象のサイクルが無ければ null を返す。
// 返す値の長さはメートル。pairs は跳びを数えた隣り合うサイクルの組の数で、間引いたデータでは 0 になる
export function filterMetrics(fix, from, to) {
  const lo = Math.max(0, from);
  const hi = Math.min(fix.t.length - 1, to);
  const ls = { x: [], y: [], steps: [] };
  const kf = { x: [], y: [], steps: [] };
  let rejected = 0;
  let predicted = 0;
  let prev = -1;
  for (let i = lo; i <= hi; i++) {
    rejected += fix.krej[i] ?? 0;
    if (fix.kok[i] && !fix.kupd[i]) predicted++;
    if (!(fix.ok[i] && fix.kok[i])) continue;
    if (prev >= 0 && fix.seq[i] - fix.seq[prev] === 1) {
      ls.steps.push(Math.hypot(fix.x[i] - fix.x[prev], fix.y[i] - fix.y[prev]));
      kf.steps.push(Math.hypot(fix.kx[i] - fix.kx[prev], fix.ky[i] - fix.ky[prev]));
    }
    ls.x.push(fix.x[i]);
    ls.y.push(fix.y[i]);
    kf.x.push(fix.kx[i]);
    kf.y.push(fix.ky[i]);
    prev = i;
  }
  if (ls.x.length === 0) return null;
  return {
    cycles: hi - lo + 1,
    compared: ls.x.length,
    pairs: ls.steps.length,
    rejected,
    predicted,
    ls: positionStats(ls.x, ls.y, ls.steps),
    kf: positionStats(kf.x, kf.y, kf.steps),
  };
}

// フィルタの値 / 最小二乗の値。どちらかが無いか、最小二乗が 0 なら null
export function ratio(kfValue, lsValue) {
  if (kfValue === null || lsValue === null || kfValue === undefined || lsValue === undefined) return null;
  return lsValue > 0 ? kfValue / lsValue : null;
}
