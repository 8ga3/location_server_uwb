// XY 平面の描画 (Canvas)。アンカー位置、軌跡 (時刻で色付け)、選択時刻の測距円、測位失敗点の × を描く。
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

export class XYPlot {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
  }

  // view: { data, colors, fromIndex, toIndex, cursorIndex, ended, fitTrail }
  // fromIndex..toIndex (両端を含む) の fix を軌跡として描き、cursorIndex を現在位置とする
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
    };
    const { data } = view;
    const fix = data.fix;
    const from = Math.max(0, view.fromIndex);
    const to = Math.min(fix.t.length - 1, view.toIndex);

    const frame = this._frame(view, from, to, width, height);
    this._grid(frame, colors, width, height);

    const cursor = view.cursorIndex;
    if (cursor >= 0 && cursor < fix.t.length) this._circles(view, frame, cursor);
    const outside = this._trail(view, frame, from, to, colors);
    this._anchors(view, frame, colors);
    if (cursor >= 0 && cursor < fix.t.length) this._marker(view, frame, cursor, colors);

    if (outside > 0) {
      ctx.fillStyle = colors.bad;
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "right";
      ctx.textBaseline = "top";
      ctx.fillText(`表示範囲外の点: ${outside}`, width - 8, 8);
    }
  }

  // 表示範囲を決め、ワールド座標 (m) から画面座標への変換を返す
  _frame(view, from, to, width, height) {
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
    for (let i = from; i <= to; i++) {
      const x = fix.x[i];
      const y = fix.y[i];
      if (x === null || y === null) continue;
      if (limit && (x < limit.minX || x > limit.maxX || y < limit.minY || y > limit.maxY)) continue;
      extend(box, x, y);
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
    const fix = data.fix;
    const good = data.lastGoodBefore(cursor);
    const tagZ = good >= 0 ? fix.z[good] : null;
    const ranges = data.rangesNear(fix.t[cursor]);
    ctx.lineWidth = 1.5;
    for (const [id, r] of ranges) {
      const anchor = data.anchorById(id);
      if (!anchor || r.d === null || r.st !== 0) continue;
      const dz = tagZ === null ? 0 : anchor.z - tagZ;
      const horizontal = Math.sqrt(Math.max(0, r.d * r.d - dz * dz));
      ctx.strokeStyle = colors.get(id) ?? "#888";
      ctx.globalAlpha = 0.8;
      ctx.beginPath();
      ctx.arc(frame.toX(anchor.x), frame.toY(anchor.y), horizontal * frame.scale, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }

  // 軌跡。古い点ほど青く薄く、新しい点ほど赤く濃く描く。表示範囲外の点の数を返す
  _trail(view, frame, from, to, colors) {
    const { ctx } = this;
    const fix = view.data.fix;
    const count = to - from;
    let outside = 0;
    let prev = null;
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
        prev = null;
        continue;
      }
      good = i;
      const x = fix.x[i];
      const y = fix.y[i];
      if (!frame.inView(x, y)) {
        outside++;
        prev = null;
        continue;
      }
      const px = frame.toX(x);
      const py = frame.toY(y);
      const color = trailColor(ratio, 0.25 + 0.75 * ratio);
      if (prev) {
        ctx.strokeStyle = color;
        ctx.beginPath();
        ctx.moveTo(prev[0], prev[1]);
        ctx.lineTo(px, py);
        ctx.stroke();
      }
      ctx.fillStyle = color;
      ctx.fillRect(px - 1.5, py - 1.5, 3, 3);
      prev = [px, py];
    }
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

  // 現在位置のマーカー。セッションが終わっていればグレーで残す
  _marker(view, frame, cursor, colors) {
    const { ctx } = this;
    const fix = view.data.fix;
    const good = view.data.lastGoodBefore(cursor);
    if (good < 0) return;
    const x = fix.x[good];
    const y = fix.y[good];
    if (!frame.inView(x, y)) return;
    const px = frame.toX(x);
    const py = frame.toY(y);
    ctx.fillStyle = view.ended ? colors.ended : colors.marker;
    ctx.strokeStyle = colors.fg;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(px, py, 7, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = colors.fg;
    ctx.font = "12px ui-monospace, Menlo, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText(`(${x.toFixed(2)}, ${y.toFixed(2)})`, px + 10, py + 6);
  }
}
