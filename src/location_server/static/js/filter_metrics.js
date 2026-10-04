// フィルタの効果の指標 (設計文書 8.4)。最小二乗の解とフィルタ後の位置を、どちらも有効なサイクルどうしで比べる。
// tools/filter_compare.py も同じ定義で計算するので、定義を変えるときは両方を揃える。

// 跳びとして数えるしきい値 [mm]。座標はタグが整数ミリメートルで送るので、跳びの判定も整数ミリメートルの差で行う。
// メートルの浮動小数のまま比べると、ちょうど 50 mm の移動が丸め誤差で 50 mm を超えたと数えられる
export const JUMP_THRESHOLD_MM = 50;

// サイクル p から i への水平の移動量の 2 乗 [mm^2]。API のメートルを整数ミリメートルへ戻してから差を取る
function stepSqMm(xs, ys, i, p) {
  const dx = Math.round(xs[i] * 1000) - Math.round(xs[p] * 1000);
  const dy = Math.round(ys[i] * 1000) - Math.round(ys[p] * 1000);
  return dx * dx + dy * dy;
}

// steps は跳びの 2 乗 [mm^2] の列。返す長さはメートル
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
  let stepMaxSq = null;
  let jumps = 0;
  for (const s of steps) {
    stepSq += s;
    if (stepMaxSq === null || s > stepMaxSq) stepMaxSq = s;
    if (s > JUMP_THRESHOLD_MM * JUMP_THRESHOLD_MM) jumps++;
  }
  return {
    meanX: mx,
    meanY: my,
    scatterRms: Math.sqrt(sq / n),
    stepRms: steps.length > 0 ? Math.sqrt(stepSq / steps.length) / 1000 : null,
    stepMax: stepMaxSq === null ? null : Math.sqrt(stepMaxSq) / 1000,
    jumps: steps.length > 0 ? jumps : null,
  };
}

// fix の from..to (両端を含む) の範囲で指標を求める。対象のサイクルが無ければ null を返す。
// 返す値の長さはメートル。pairs は跳びを数えた隣り合うサイクルの組の数。
// 間引いたデータには steps: false を渡し、跳びを数えない。参照 API は間引いても末尾の行を必ず残すので、
// 最後の 2 行だけ seq が続くことがあり、その 1 組だけで範囲全体の跳びを出してしまうため
export function filterMetrics(fix, from, to, { steps = true } = {}) {
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
    if (steps && prev >= 0 && fix.seq[i] - fix.seq[prev] === 1) {
      ls.steps.push(stepSqMm(fix.x, fix.y, i, prev));
      kf.steps.push(stepSqMm(fix.kx, fix.ky, i, prev));
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
