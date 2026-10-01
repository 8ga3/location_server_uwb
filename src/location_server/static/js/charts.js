// 時系列グラフ (uPlot)。距離 (アンカーごと)、周期時間、測位の品質 (残差・使用アンカー数・フィルタの σ と
// 棄却数) の 3 枚を縦に並べる。
// 横軸はタグの millis() を秒にしたもの。測距の失敗は null として線を切り、欠測として見せる。
// uPlot は index.html で読み込む同梱ファイルが定義するグローバル変数を使う。

import { cssVar } from "./colors.js";

/* global uPlot */

const HEIGHT_RANGE = 200;
const HEIGHT_SMALL = 130;
const SYNC_KEY = "uwb";

function seconds(ts) {
  return ts.map((t) => t / 1000);
}

export class TimeCharts {
  // handlers: { onPick(tMs), onZoom(minMs, maxMs), onReset() }
  constructor(elements, handlers) {
    this.el = elements;
    this.handlers = handlers;
    this.anchorIds = [];
    this.colors = new Map();
    this.cursorT = null;
    this.charts = [];
    this._programmatic = 0;
    this._build();
    const observer = new ResizeObserver(() => this.resize());
    observer.observe(elements.range.parentElement);
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => this._build());
  }

  setAnchors(ids, colors) {
    const same = ids.length === this.anchorIds.length && ids.every((id, i) => id === this.anchorIds[i]);
    this.colors = colors;
    if (same) return;
    this.anchorIds = ids;
    this._build();
  }

  // data: SessionData。xMin / xMax (ms) を指定すると横軸をその範囲に固定する
  update(data, xMin, xMax) {
    this._lastArgs = [data, xMin, xMax];
    if (this.charts.length === 0) return;
    const fix = data.fix;
    const xs = seconds(fix.t);

    const tables = this.anchorIds.map((id) => {
      const columns = data.ranges.get(id);
      return columns ? [seconds(columns.t), columns.d] : [[], []];
    });
    let rangeData;
    if (tables.length === 0) {
      rangeData = [xs];
    } else {
      // アンカーごとに時刻が揃わない (再生時の間引き) 場合があるので、横軸を合わせてから渡す。
      // 揃えるために足した位置は undefined になり、線は切れずにつながる。測距の失敗 (null) だけで線が切れる
      rangeData = uPlot.join(tables);
    }
    // 周期はサーバーが間引く前に求めた値 (dt) を使う。間引いた再生データでも本来の周期が見える
    const periodData = [xs, fix.dt];
    const toMm = (v) => (v === null || v === undefined ? null : v * 1000);
    const qualityData = [xs, fix.resid.map(toMm), fix.used, fix.ksig.map(toMm), fix.krej];

    this._programmatic++;
    try {
      const datasets = [rangeData, periodData, qualityData];
      this.charts.forEach((chart, i) => {
        chart.setData(datasets[i]);
        if (xMin !== null && xMax !== null && xMax > xMin) {
          chart.setScale("x", { min: xMin / 1000, max: xMax / 1000 });
        }
      });
    } finally {
      this._programmatic--;
    }
  }

  setCursor(tMs) {
    if (tMs === this.cursorT) return;
    this.cursorT = tMs;
    // 軸も計算し直させる。作り直した直後のグラフは軸がまだ計算されておらず、
    // redraw(false, false) では uPlot が未計算の目盛りを読んで例外になる
    for (const chart of this.charts) chart.redraw(false, true);
  }

  resize() {
    const width = this._width();
    this.charts.forEach((chart, i) => chart.setSize({ width, height: i === 0 ? HEIGHT_RANGE : HEIGHT_SMALL }));
  }

  _width() {
    return Math.max(240, this.el.range.parentElement.clientWidth - 4);
  }

  _build() {
    for (const chart of this.charts) chart.destroy();
    this.charts = [];
    const muted = cssVar("--fg-muted");
    const grid = cssVar("--grid");
    const fg = cssVar("--fg");
    const marker = cssVar("--marker");
    const width = this._width();

    const axis = (label, extra = {}) => ({
      label,
      stroke: muted,
      labelSize: 18,
      size: 48,
      grid: { stroke: grid, width: 1 },
      ticks: { stroke: grid, width: 1 },
      ...extra,
    });
    const xAxis = axis("タグ時刻 [s]", { size: 36 });

    const cursorLine = (u) => {
      if (this.cursorT === null) return;
      const x = u.valToPos(this.cursorT / 1000, "x", true);
      if (!Number.isFinite(x)) return;
      const { ctx } = u;
      ctx.save();
      ctx.strokeStyle = marker;
      ctx.lineWidth = 1.5 * (window.devicePixelRatio || 1);
      ctx.beginPath();
      ctx.moveTo(x, u.bbox.top);
      ctx.lineTo(x, u.bbox.top + u.bbox.height);
      ctx.stroke();
      ctx.restore();
    };

    const onSetScale = (u, key) => {
      if (key !== "x" || this._programmatic > 0) return;
      const { min, max } = u.scales.x;
      if (min !== null && max !== null) this.handlers.onZoom(min * 1000, max * 1000);
    };

    const attachPick = (u) => {
      let down = null;
      u.over.addEventListener("mousedown", (e) => {
        down = [e.clientX, e.clientY];
      });
      u.over.addEventListener("mouseup", (e) => {
        if (!down) return;
        const moved = Math.hypot(e.clientX - down[0], e.clientY - down[1]);
        down = null;
        if (moved < 3) this.handlers.onPick(u.posToVal(e.offsetX, "x") * 1000);
      });
    };

    const base = (title, height, series, axes, extraScales = {}) => ({
      title,
      width,
      height,
      series,
      axes,
      scales: { x: { time: false }, ...extraScales },
      legend: { live: true },
      cursor: {
        sync: { key: SYNC_KEY },
        drag: { x: true, y: false },
        bind: {
          dblclick: () => () => {
            this.handlers.onReset();
            return null;
          },
        },
      },
      hooks: { draw: [cursorLine], setScale: [onSetScale], ready: [attachPick] },
    });

    const rangeSeries = [
      { label: "t [s]", value: (u, v) => (v === null || v === undefined ? "--" : v.toFixed(2)) },
      ...this.anchorIds.map((id) => ({
        label: id,
        stroke: this.colors.get(id) ?? fg,
        width: 1.5,
        spanGaps: false,
        points: { show: false },
        value: (u, v) => (v === null || v === undefined ? "--" : `${v.toFixed(3)} m`),
      })),
    ];
    const range = new uPlot(
      base("距離", HEIGHT_RANGE, rangeSeries, [xAxis, axis("距離 [m]")]),
      [[]].concat(this.anchorIds.map(() => [])),
      this.el.range,
    );

    const period = new uPlot(
      base(
        "周期時間",
        HEIGHT_SMALL,
        [
          { label: "t [s]" },
          { label: "周期 [ms]", stroke: cssVar("--accent"), width: 1.5, spanGaps: false, points: { show: false } },
        ],
        [xAxis, axis("[ms]")],
      ),
      [[], []],
      this.el.period,
    );

    const quality = new uPlot(
      base(
        "残差・使用数・フィルタの σ",
        HEIGHT_SMALL,
        [
          { label: "t [s]" },
          { label: "残差 [mm]", stroke: cssVar("--warn"), width: 1.5, spanGaps: false, points: { show: false } },
          {
            label: "使用数",
            scale: "n",
            stroke: muted,
            width: 1,
            spanGaps: false,
            points: { show: false },
            paths: uPlot.paths.stepped({ align: 1 }),
          },
          {
            label: "フィルタ σ [mm]",
            stroke: cssVar("--filter"),
            width: 1.5,
            spanGaps: false,
            points: { show: false },
          },
          {
            label: "棄却数",
            scale: "n",
            stroke: cssVar("--bad"),
            width: 1,
            spanGaps: false,
            points: { show: false },
            paths: uPlot.paths.stepped({ align: 1 }),
          },
        ],
        [xAxis, axis("[mm]"), axis("本数", { side: 1, scale: "n", incrs: [1, 2, 5], grid: { show: false } })],
        { n: { range: (u, min, max) => [0, Math.max(4, max ?? 4) + 0.5] } },
      ),
      [[], [], [], [], []],
      this.el.quality,
    );

    this.charts = [range, period, quality];
    if (this._lastArgs) this.update(...this._lastArgs);
  }
}
