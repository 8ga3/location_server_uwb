// XY 平面の描画 (Canvas)。アンカー位置、軌跡 (時刻で色付け)、選択時刻の測距円、測位失敗点の × を描く。
// タグ側のカルマンフィルタの軌跡は、最小二乗の軌跡に単色 (--filter) で重ねる。
// 座標系はアンカー 0 を原点とする右手系で、+Y を画面の上に取る。X と Y の縮尺は揃える。

import { cssVar, trailColor } from "./colors.js";

// 目盛り幅を 1 / 2 / 5 × 10^n から選び、線の数を targetLines 前後に保つ。
// 発散した解 (最大で約 2147 km) を表示範囲に含めても、線が数千本にならないようにする
export function niceStep(span, targetLines) {
  const raw = span / targetLines;
  if (!(raw > 0) || !Number.isFinite(raw)) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  for (const factor of [1, 2, 5, 10]) {
    if (factor * magnitude >= raw) return factor * magnitude;
  }
  return 10 * magnitude;
}

function extend(box, x, y) {
  box.minX = Math.min(box.minX, x);
  box.maxX = Math.max(box.maxX, x);
  box.minY = Math.min(box.minY, y);
  box.maxY = Math.max(box.maxY, y);
}

function emptyBox() {
  return { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity };
}

function isEmpty(box) {
  return !(box.minX <= box.maxX);
}

// 測距円の半径 (アンカーとタグの高さの差を除いた水平距離)。円にできない値は null を返す。
// 負の距離や高さの差より短い距離は、二乗や 0 への丸めで「もっともらしい円」に見えてしまうので描かない。
// デバッグで見たい異常値なので、表と時系列には生の値のまま残す
export function horizontalRange(d, dz) {
  if (d === null || d === undefined || !(d >= 0)) return null;
  const h2 = d * d - dz * dz;
  return h2 >= 0 ? Math.sqrt(h2) : null;
}

// 選択時刻に測位していた高さ。測位に失敗したサイクルでは直前に成功していた値を使う
export function tagHeightAt(data, index) {
  const good = data.lastGoodBefore(index);
  return good >= 0 ? data.fix.z[good] : null;
}

export class XYPlot {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
  }

  // view: { data, colors, fromIndex, toIndex, cursorIndex, ended, fitTrail, showLS, showFilter }
  // fromIndex..toIndex (両端を含む) の fix を軌跡として描き、cursorIndex を現在位置とする。
  // showLS / showFilter は最小二乗とフィルタの軌跡・マーカーをそれぞれ描くかどうか (省略時は描く)
  render(view) {
    const { canvas, ctx } = this;
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (width === 0 || height === 0) return;
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const colors = {
      fg: cssVar("--fg"),
      muted: cssVar("--fg-muted"),
      grid: cssVar("--grid"),
      marker: cssVar("--marker"),
      ended: cssVar("--ended"),
      bad: cssVar("--bad"),
      filter: cssVar("--filter"),
      panel: cssVar("--panel"),
    };
    const { data } = view;
    const fix = data.fix;
    const from = Math.max(0, view.fromIndex);
    const to = Math.min(fix.t.length - 1, view.toIndex);
    const showLS = view.showLS ?? true;
    const showFilter = view.showFilter ?? true;

    const frame = this._frame(view, from, to, width, height, showLS, showFilter);
    this._grid(frame, colors, width, height);

    const cursor = view.cursorIndex;
    if (cursor >= 0 && cursor < fix.t.length) this._circles(view, frame, cursor);
    let outside = 0;
    if (showLS) outside += this._trail(view, frame, from, to, colors);
    if (showFilter) outside += this._filterTrail(view, frame, from, to, colors);
    this._anchors(view, frame, colors);
    if (cursor >= 0 && cursor < fix.t.length) this._markers(view, frame, cursor, colors, showLS, showFilter);

    if (outside > 0) {
      ctx.fillStyle = colors.bad;
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "right";
      ctx.textBaseline = "top";
      ctx.fillText(`表示範囲外の点: ${outside}`, width - 8, 8);
    }
  }

  // 表示範囲を決め、ワールド座標 (m) から画面座標への変換を返す。表示している軌跡の点だけを範囲に含める
  _frame(view, from, to, width, height, showLS, showFilter) {
    const { data } = view;
    const anchorsBox = emptyBox();
    for (const a of data.anchors) extend(anchorsBox, a.x, a.y);
    const box = isEmpty(anchorsBox) ? emptyBox() : { ...anchorsBox };
    // アンカーから大きく外れた点 (発散した解) まで入れると画面が潰れるので、既定ではアンカーの
    // 外接矩形をその幅だけ (最低 2 m) 広げた範囲に入る点だけを表示範囲の計算に使う
    let limit = null;
    if (!view.fitTrail && !isEmpty(anchorsBox)) {
      const margin = Math.max(2, anchorsBox.maxX - anchorsBox.minX, anchorsBox.maxY - anchorsBox.minY);
      limit = {
        minX: anchorsBox.minX - margin,
        maxX: anchorsBox.maxX + margin,
        minY: anchorsBox.minY - margin,
        maxY: anchorsBox.maxY + margin,
      };
    }
    const fix = data.fix;
    const include = (x, y) => {
      if (x === null || y === null || x === undefined || y === undefined) return;
      if (limit && (x < limit.minX || x > limit.maxX || y < limit.minY || y > limit.maxY)) return;
      extend(box, x, y);
    };
    for (let i = from; i <= to; i++) {
      if (showLS) include(fix.x[i], fix.y[i]);
      // フィルタの位置は最小二乗の成否と関係なく、kok が真のサイクルだけ値を持つ
      if (showFilter && fix.kok[i]) include(fix.kx[i], fix.ky[i]);
    }
    if (isEmpty(box)) {
      extend(box, 0, 0);
      extend(box, 5, 5);
    }

    let spanX = Math.max(1, box.maxX - box.minX);
    let spanY = Math.max(1, box.maxY - box.minY);
    const cx = (box.minX + box.maxX) / 2;
    const cy = (box.minY + box.maxY) / 2;
    spanX *= 1.16;
    spanY *= 1.16;
    const pad = 28;
    const scale = Math.min((width - pad * 2) / spanX, (height - pad * 2) / spanY);
    return {
      scale,
      toX: (x) => width / 2 + (x - cx) * scale,
      toY: (y) => height / 2 - (y - cy) * scale,
      fromX: (px) => cx + (px - width / 2) / scale,
      fromY: (py) => cy - (py - height / 2) / scale,
      inView: (x, y) => {
        const px = width / 2 + (x - cx) * scale;
        const py = height / 2 - (y - cy) * scale;
        return px >= 0 && px <= width && py >= 0 && py <= height;
      },
    };
  }

  _grid(frame, colors, width, height) {
    const { ctx } = this;
    const minX = frame.fromX(0);
    const maxX = frame.fromX(width);
    const minY = frame.fromY(height);
    const maxY = frame.fromY(0);
    const step = niceStep(Math.max(maxX - minX, maxY - minY), 8);
    ctx.lineWidth = 1;
    ctx.font = "11px system-ui, sans-serif";
    ctx.fillStyle = colors.muted;
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    const digits = step < 1 ? Math.min(3, Math.ceil(-Math.log10(step))) : 0;
    for (let x = Math.ceil(minX / step) * step; x <= maxX; x += step) {
      const px = Math.round(frame.toX(x)) + 0.5;
      ctx.strokeStyle = Math.abs(x) < step / 2 ? colors.muted : colors.grid;
      ctx.beginPath();
      ctx.moveTo(px, 0);
      ctx.lineTo(px, height);
      ctx.stroke();
      ctx.fillText(`${x.toFixed(digits)}`, px + 2, height - 14);
    }
    for (let y = Math.ceil(minY / step) * step; y <= maxY; y += step) {
      const py = Math.round(frame.toY(y)) + 0.5;
      ctx.strokeStyle = Math.abs(y) < step / 2 ? colors.muted : colors.grid;
      ctx.beginPath();
      ctx.moveTo(0, py);
      ctx.lineTo(width, py);
      ctx.stroke();
      ctx.fillText(`${y.toFixed(digits)}`, 2, py + 2);
    }
    ctx.textAlign = "right";
    ctx.fillText("[m]", width - 4, height - 14);
  }

  // 選択時刻の測距円。アンカーとタグの高さの差を引いた水平距離を半径にする
  _circles(view, frame, cursor) {
    const { ctx } = this;
    const { data, colors } = view;
    const tagZ = tagHeightAt(data, cursor);
    // 一度も測位できていないとタグの高さがわからない。高さの差を 0 とみなすと 3 次元の距離を
    // 水平の半径として描いてしまい、もっともらしい誤った円になるので、高さがわかるまでは描かない
    if (tagZ === null) return;
    const ranges = data.rangesAtIndex(cursor);
    ctx.lineWidth = 1.5;
    for (const [id, r] of ranges) {
      const anchor = data.anchorById(id);
      if (!anchor || r.d === null || r.st !== 0) continue;
      const horizontal = horizontalRange(r.d, anchor.z - tagZ);
      if (horizontal === null) continue;
      ctx.strokeStyle = colors.get(id) ?? "#888";
      ctx.globalAlpha = 0.8;
      ctx.beginPath();
      ctx.arc(frame.toX(anchor.x), frame.toY(anchor.y), horizontal * frame.scale, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }

  // 軌跡。古い点ほど青く薄く、新しい点ほど赤く濃く描く。表示範囲外の点の数を返す。
  // 軌跡は一本につなげて描く。ただし実測でつながっていない区間、つまり UDP の欠測 (dt が null) や
  // 測位の失敗をまたぐ区間は点線にして、実在する移動経路と見分けられるようにする。
  // 表示範囲外の点 (発散した解) の前後だけは線を切る。画面の外へ向かう長い線で軌跡が見えなくなるため
  _trail(view, frame, from, to, colors) {
    const { ctx } = this;
    const fix = view.data.fix;
    const count = to - from;
    let outside = 0;
    let prev = null;
    // 直前に描いた点から今の点までの間に、測位の失敗を挟んだか
    let bridged = false;
    let good = view.data.lastGoodBefore(from - 1);
    ctx.lineWidth = 1.5;
    for (let i = from; i <= to; i++) {
      const ratio = count > 0 ? (i - from) / count : 1;
      if (!fix.ok[i]) {
        // 測位に失敗したサイクルは、直前に成功していた位置に × を描く
        if (good >= 0 && frame.inView(fix.x[good], fix.y[good])) {
          const px = frame.toX(fix.x[good]);
          const py = frame.toY(fix.y[good]);
          ctx.strokeStyle = colors.bad;
          ctx.beginPath();
          ctx.moveTo(px - 4, py - 4);
          ctx.lineTo(px + 4, py + 4);
          ctx.moveTo(px + 4, py - 4);
          ctx.lineTo(px - 4, py + 4);
          ctx.stroke();
        }
        bridged = true;
        continue;
      }
      good = i;
      const x = fix.x[i];
      const y = fix.y[i];
      if (!frame.inView(x, y)) {
        outside++;
        prev = null;
        bridged = false;
        continue;
      }
      const px = frame.toX(x);
      const py = frame.toY(y);
      const color = trailColor(ratio, 0.25 + 0.75 * ratio);
      if (prev) {
        const gap = bridged || fix.dt[i] === null;
        ctx.strokeStyle = color;
        ctx.setLineDash(gap ? [4, 4] : []);
        ctx.beginPath();
        ctx.moveTo(prev[0], prev[1]);
        ctx.lineTo(px, py);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      bridged = false;
      ctx.fillStyle = color;
      ctx.fillRect(px - 1.5, py - 1.5, 3, 3);
      prev = [px, py];
    }
    return outside;
  }

  // フィルタの軌跡。単色で、古い点ほど薄く描く。表示範囲外の点の数を返す。
  // フィルタの位置が無効なサイクル (kok が偽) では線を切る。予測だけで進んだ点 (kupd が偽) は白抜きにし、
  // そこへ向かう区間を点線にする。UDP の欠測 (dt が null) をまたぐ区間と、初期化した点 (kinit) へ向かう区間も
  // 実測でつながっていないので点線にする。初期化した点には輪を重ねる。
  _filterTrail(view, frame, from, to, colors) {
    const { ctx } = this;
    const fix = view.data.fix;
    const count = to - from;
    let outside = 0;
    let prev = null;
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = colors.filter;
    ctx.fillStyle = colors.filter;
    for (let i = from; i <= to; i++) {
      if (!fix.kok[i]) {
        prev = null;
        continue;
      }
      const x = fix.kx[i];
      const y = fix.ky[i];
      if (!frame.inView(x, y)) {
        outside++;
        prev = null;
        continue;
      }
      const ratio = count > 0 ? (i - from) / count : 1;
      ctx.globalAlpha = 0.3 + 0.7 * ratio;
      const px = frame.toX(x);
      const py = frame.toY(y);
      const predicted = !fix.kupd[i];
      if (prev) {
        const gap = predicted || fix.kinit[i] || fix.dt[i] === null;
        ctx.setLineDash(gap ? [4, 4] : []);
        ctx.beginPath();
        ctx.moveTo(prev[0], prev[1]);
        ctx.lineTo(px, py);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.beginPath();
      ctx.arc(px, py, predicted ? 2.5 : 2, 0, Math.PI * 2);
      if (predicted) {
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.lineWidth = 1.5;
      } else {
        ctx.fill();
      }
      if (fix.kinit[i]) {
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(px, py, 6, 0, Math.PI * 2);
        ctx.stroke();
        ctx.lineWidth = 1.5;
      }
      prev = [px, py];
    }
    ctx.globalAlpha = 1;
    return outside;
  }

  _anchors(view, frame, colors) {
    const { ctx } = this;
    ctx.font = "12px system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    for (const anchor of view.data.anchors) {
      const px = frame.toX(anchor.x);
      const py = frame.toY(anchor.y);
      ctx.fillStyle = view.colors.get(anchor.id) ?? colors.fg;
      ctx.fillRect(px - 6, py - 6, 12, 12);
      ctx.strokeStyle = colors.fg;
      ctx.lineWidth = 1;
      ctx.strokeRect(px - 6, py - 6, 12, 12);
      ctx.fillStyle = colors.fg;
      const name = anchor.label ? `${anchor.id} ${anchor.label}` : anchor.id;
      ctx.fillText(name, px + 9, py - 4);
    }
  }

  // 現在位置のマーカー。最小二乗は円、フィルタは菱形で描き、セッションが終わっていればグレーで残す。
  // 座標のラベルは、画面上で下にあるマーカーの下側と、上にあるマーカーの上側へ振り分けて重ならないようにする
  _markers(view, frame, cursor, colors, showLS, showFilter) {
    const { ctx } = this;
    const { data } = view;
    const fix = data.fix;
    let ls = null;
    let kf = null;
    if (showLS) {
      const good = data.lastGoodBefore(cursor);
      if (good >= 0 && frame.inView(fix.x[good], fix.y[good])) {
        ls = { x: fix.x[good], y: fix.y[good], px: frame.toX(fix.x[good]), py: frame.toY(fix.y[good]) };
      }
    }
    if (showFilter) {
      const good = data.lastFilterBefore(cursor);
      if (good >= 0 && frame.inView(fix.kx[good], fix.ky[good])) {
        kf = { x: fix.kx[good], y: fix.ky[good], px: frame.toX(fix.kx[good]), py: frame.toY(fix.ky[good]) };
      }
    }
    // 既定では最小二乗のラベルを下、フィルタのラベルを上に置く。フィルタのほうが画面の下にあれば入れ替える
    const lsBelow = !(ls && kf && kf.py > ls.py);

    ctx.strokeStyle = colors.fg;
    ctx.lineWidth = 1.5;
    if (ls) {
      ctx.fillStyle = view.ended ? colors.ended : colors.marker;
      ctx.beginPath();
      ctx.arc(ls.px, ls.py, 7, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
    if (kf) {
      ctx.fillStyle = view.ended ? colors.ended : colors.filter;
      ctx.beginPath();
      ctx.moveTo(kf.px, kf.py - 8);
      ctx.lineTo(kf.px + 8, kf.py);
      ctx.lineTo(kf.px, kf.py + 8);
      ctx.lineTo(kf.px - 8, kf.py);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
    }

    // 軌跡の上に重なっても読めるよう、ラベルは背景色で縁取ってから描く
    ctx.font = "12px ui-monospace, Menlo, monospace";
    ctx.textAlign = "left";
    ctx.lineJoin = "round";
    const label = (m, text, below) => {
      const x = m.px + 11;
      const y = below ? m.py + 6 : m.py - 6;
      ctx.textBaseline = below ? "top" : "bottom";
      ctx.strokeStyle = colors.panel;
      ctx.lineWidth = 3;
      ctx.strokeText(text, x, y);
      ctx.fillStyle = colors.fg;
      ctx.fillText(text, x, y);
    };
    if (ls) label(ls, `(${ls.x.toFixed(2)}, ${ls.y.toFixed(2)})`, lsBelow);
    if (kf) label(kf, `フィルタ (${kf.x.toFixed(2)}, ${kf.y.toFixed(2)})`, !lsBelow || !ls);
  }
}
