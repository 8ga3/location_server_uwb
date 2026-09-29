// 色の取り決め。アンカーの色は XY 平面と時系列グラフで揃える

// 色覚の違いでも区別しやすい並び (Okabe-Ito を基にした)
const ANCHOR_PALETTE = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#8C6D31", "#7F7F7F"];

export function anchorColors(ids) {
  const colors = new Map();
  ids.forEach((id, i) => colors.set(id, ANCHOR_PALETTE[i % ANCHOR_PALETTE.length]));
  return colors;
}

// CSS 変数の値を読む。Canvas と uPlot はページのテーマに合わせてこれで色を取る
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// 軌跡の時刻による色付け。0 (古い) は青、1 (新しい) は赤
export function trailColor(ratio, alpha = 1) {
  const r = Math.min(1, Math.max(0, ratio));
  const hue = 220 - 220 * r;
  return `hsla(${hue.toFixed(0)}, 75%, 50%, ${alpha})`;
}
