// 描画するデータの入れ物。ライブ (WebSocket) と再生 (参照 API) のどちらから来たデータも、
// サーバーが返す列指向の形 (fix / ranges) のままここへ積み、描画側はこれだけを見る。
// 列の意味は src/location_server/columns.py を参照。t はタグの millis()、長さはメートル。
// k で始まる列はタグ側のカルマンフィルタの出力で、最小二乗の列 (x / y / z / ok) とは独立に入る。

export const FIX_KEYS = [
  "t", "seq", "dt", "x", "y", "z", "ok", "used", "resid",
  "kx", "ky", "kz", "kok", "kupd", "kinit", "ksig", "kused", "krej",
];
export const RANGE_KEYS = ["t", "seq", "d", "st", "el", "kf"];

function emptyColumns(keys) {
  const columns = {};
  for (const key of keys) columns[key] = [];
  return columns;
}

function appendColumns(target, source, keys) {
  const n = source.t.length;
  for (const key of keys) {
    const src = source[key];
    const dst = target[key];
    for (let i = 0; i < n; i++) dst.push(src[i]);
  }
}

function dropHead(columns, keys, count) {
  if (count <= 0) return;
  for (const key of keys) columns[key].splice(0, count);
}

// 昇順の配列 values で value 以下の最後の位置。無ければ -1
export function lastIndexAtOrBefore(values, value) {
  let lo = 0;
  let hi = values.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (values[mid] <= value) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found;
}

// 昇順の配列 values で value に最も近い位置。空なら -1
export function nearestIndex(values, value) {
  if (values.length === 0) return -1;
  const i = lastIndexAtOrBefore(values, value);
  if (i < 0) return 0;
  if (i >= values.length - 1) return values.length - 1;
  return value - values[i] <= values[i + 1] - value ? i : i + 1;
}

export class SessionData {
  constructor() {
    this.anchors = [];
    this.anchorsRev = null;
    this.clear();
  }

  // 計測値とセッション情報を捨てる。アンカー座標は残す
  clear() {
    this.info = { session_id: null, tag_id: null, boot_id: null, config_rev: null, active: false };
    this.fix = emptyColumns(FIX_KEYS);
    this.ranges = new Map();
  }

  setInfo(frame) {
    for (const key of Object.keys(this.info)) {
      if (key in frame) this.info[key] = frame[key];
    }
  }

  setAnchors(anchors, rev) {
    this.anchors = anchors ?? [];
    this.anchorsRev = rev ?? null;
  }

  // 列指向のデータを末尾へ足す。ライブでは同じ seq 以下のサイクルが来ても重ねない
  append(fix, ranges) {
    if (fix && fix.t.length > 0) {
      const last = this.lastSeq();
      let skip = 0;
      if (last !== null) {
        while (skip < fix.seq.length && fix.seq[skip] <= last) skip++;
      }
      if (skip === 0) {
        appendColumns(this.fix, fix, FIX_KEYS);
      } else if (skip < fix.seq.length) {
        const sliced = {};
        for (const key of FIX_KEYS) sliced[key] = fix[key].slice(skip);
        appendColumns(this.fix, sliced, FIX_KEYS);
      }
    }
    for (const [id, columns] of Object.entries(ranges ?? {})) {
      let target = this.ranges.get(id);
      if (!target) {
        target = emptyColumns(RANGE_KEYS);
        this.ranges.set(id, target);
      }
      const last = target.seq.length ? target.seq[target.seq.length - 1] : null;
      let skip = 0;
      if (last !== null) {
        while (skip < columns.seq.length && columns.seq[skip] <= last) skip++;
      }
      if (skip >= columns.seq.length) continue;
      const sliced = {};
      for (const key of RANGE_KEYS) sliced[key] = skip ? columns[key].slice(skip) : columns[key];
      appendColumns(target, sliced, RANGE_KEYS);
    }
  }

  // t が tMin より前のデータを捨てる (ライブのトレイル用)
  trimBefore(tMin) {
    dropHead(this.fix, FIX_KEYS, lastIndexAtOrBefore(this.fix.t, tMin - 1) + 1);
    for (const columns of this.ranges.values()) {
      dropHead(columns, RANGE_KEYS, lastIndexAtOrBefore(columns.t, tMin - 1) + 1);
    }
  }

  lastSeq() {
    const seq = this.fix.seq;
    return seq.length ? seq[seq.length - 1] : null;
  }

  firstT() {
    return this.fix.t.length ? this.fix.t[0] : null;
  }

  lastT() {
    const t = this.fix.t;
    return t.length ? t[t.length - 1] : null;
  }

  // 構成に載っているアンカーと、測距の記録があるアンカーを合わせた ID の一覧
  anchorIds() {
    const ids = new Set(this.anchors.map((a) => a.id));
    for (const id of this.ranges.keys()) ids.add(id);
    return [...ids].sort();
  }

  anchorById(id) {
    return this.anchors.find((a) => a.id === id) ?? null;
  }

  // 時刻 t に最も近いサイクルの位置 (fix の添字)
  indexNear(t) {
    return nearestIndex(this.fix.t, t);
  }

  // fix の index 番目のサイクルと同じ seq の測距をアンカーごとに返す。
  // そのサイクルに測距が無いアンカーは含めない (欠測)。時刻の近い別のサイクルの測距で代用すると、
  // アンカーの構成が変わった後などに古い距離を現在値として表と測距円に出してしまうため
  rangesAtIndex(index) {
    const result = new Map();
    if (index < 0 || index >= this.fix.seq.length) return result;
    const seq = this.fix.seq[index];
    for (const [id, columns] of this.ranges) {
      const i = lastIndexAtOrBefore(columns.seq, seq);
      if (i >= 0 && columns.seq[i] === seq) {
        result.set(id, {
          t: columns.t[i], seq, d: columns.d[i], st: columns.st[i], el: columns.el[i], kf: columns.kf[i],
        });
      }
    }
    return result;
  }

  // 直前に測位が成功していた位置。測位に失敗したサイクルの × を描く場所に使う
  lastGoodBefore(index) {
    for (let i = index; i >= 0; i--) {
      if (this.fix.ok[i]) return i;
    }
    return -1;
  }

  // 直前にフィルタの位置が有効だったサイクル。フィルタの現在位置のマーカーを描く場所に使う
  lastFilterBefore(index) {
    for (let i = index; i >= 0; i--) {
      if (this.fix.kok[i]) return i;
    }
    return -1;
  }
}
