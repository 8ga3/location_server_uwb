// 可視化ページの画面制御。ライブと再生でパネルの構成は変えず、データの供給元だけを切り替える
// (設計文書 8.1)。描画は requestAnimationFrame で束ね、データが変わったときだけ描き直す。

import { TimeCharts } from "./charts.js";
import { anchorColors } from "./colors.js";
import { filterMetrics, JUMP_THRESHOLD_M, ratio } from "./filter_metrics.js";
import { lampClass, liveIndicators } from "./indicators.js";
import { lastIndexAtOrBefore, SessionData } from "./model.js";
import { LiveSource, loadSessions, loadSummary, loadWindow } from "./sources.js";
import { horizontalRange, tagHeightAt, XYPlot } from "./xyplot.js";

// ライブの軌跡は直近 30 秒のトレイルとし、古い点から消す (設計文書 8.3)
const TRAIL_MS = 30000;
const SUMMARY_INTERVAL_MS = 200;
const ZOOM_FETCH_DELAY_MS = 250;
// 送信キューで append が捨てられたときに snapshot を取り直す最短の間隔
const RESYNC_INTERVAL_MS = 2000;

const END_REASONS = {
  timeout: "タグ切断 (3 秒以上受信なし)",
  new_session: "新しいセッションへ切り替わった",
  tag_end: "タグが終了を通知した",
  ended: "セッション終了",
};

const $ = (id) => document.getElementById(id);

const data = new SessionData();
const state = {
  mode: "live",
  colors: new Map(),
  dirty: true,
  lastSummaryAt: -Infinity,
  summaryPending: false,
  live: {
    tagId: 1,
    paused: false,
    // lost はサーバーが数える、この購読で捨てた append の累計 (購読し直すと 0 に戻る)。
    // lostTotal はページを開いてからの累計、resyncPending は取り直しを待っている間 true
    lost: 0,
    lostTotal: 0,
    resyncPending: false,
    lastResyncAt: -Infinity,
    resyncTimer: null,
    lastFrameAt: null,
    endReason: null,
  },
  replay: null,
  // 再生を選んでいるセッション。読み込み中や読み込み失敗でも選択欄と表示を合わせるために、state.replay とは別に持つ
  replaySessionId: null,
};

const xy = new XYPlot($("xy"));
const charts = new TimeCharts(
  { range: $("chart-range"), period: $("chart-period"), quality: $("chart-quality"), count: $("chart-count") },
  { onPick, onZoom, onReset },
);
const live = new LiveSource({ onFrame, onStatus });

// ------------------------------------------------------------------ 表示の補助

function hex(value, width) {
  return value === null || value === undefined ? "--" : `0x${value.toString(16).toUpperCase().padStart(width, "0")}`;
}

function num(value, digits = 0, unit = "") {
  if (value === null || value === undefined || !Number.isFinite(value)) return "--";
  return `${value.toFixed(digits)}${unit}`;
}

function pct(value) {
  return value === null || value === undefined ? "--" : `${(value * 100).toFixed(1)}%`;
}

function showMessage(text) {
  $("message").textContent = text ?? "";
}

function onStatus(level, text) {
  const badge = $("conn");
  badge.className = `badge ${level}`;
  badge.textContent = text;
}

function renderStats(el, entries) {
  el.replaceChildren(
    ...entries.map(([label, value, bad]) => {
      const row = document.createElement("div");
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = value;
      if (bad) dd.className = "bad";
      row.append(dt, dd);
      return row;
    }),
  );
}

// アンカーごとの表。cells は [テキスト, ランプの class (省略可)] の配列
function renderAnchorTable(headers, rows) {
  $("anchor-head").replaceChildren(
    ...headers.map((text) => {
      const th = document.createElement("th");
      th.textContent = text;
      return th;
    }),
  );
  $("anchor-body").replaceChildren(
    ...rows.map(({ id, cells }) => {
      const tr = document.createElement("tr");
      const name = document.createElement("td");
      const swatch = document.createElement("span");
      swatch.className = "swatch";
      swatch.style.background = state.colors.get(id) ?? "#888";
      const anchor = data.anchorById(id);
      name.append(swatch, anchor?.label ? `${id} ${anchor.label}` : id);
      tr.append(name);
      for (const [text, lamp] of cells) {
        const td = document.createElement("td");
        if (lamp !== undefined) {
          const dot = document.createElement("span");
          dot.className = `lamp ${lamp}`;
          td.append(dot);
        }
        td.append(text);
        tr.append(td);
      }
      return tr;
    }),
  );
}

// フィルタの状態の表示と、強調するかどうか。初期化したサイクルは観測でも更新しているので初期化を優先する
function filterState(fix, index) {
  if (!fix.kok[index]) return ["無効", true];
  if (fix.kinit[index]) return ["初期化", false];
  if (fix.kupd[index]) return ["観測で更新", false];
  return ["予測のみ", true];
}

function mm(meters, digits = 0) {
  return num(meters === null || meters === undefined ? null : meters * 1000, digits, " mm");
}

function renderCursorInfo(index) {
  const fix = data.fix;
  if (index < 0 || index >= fix.t.length) {
    renderStats($("cursor-info"), [["時刻", "--"]]);
    return;
  }
  const [kfText, kfBad] = filterState(fix, index);
  const count = (v) => (v === null || v === undefined ? "--" : String(v));
  renderStats($("cursor-info"), [
    ["タグ時刻", num(fix.t[index] / 1000, 3, " s")],
    ["seq", String(fix.seq[index])],
    ["測位", fix.ok[index] ? "成功" : "失敗", !fix.ok[index]],
    ["X", num(fix.x[index], 3, " m")],
    ["Y", num(fix.y[index], 3, " m")],
    ["使用数", num(fix.used[index])],
    ["残差", mm(fix.resid[index])],
    ["フィルタ", kfText, kfBad],
    ["フィルタ X", num(fix.kx[index], 3, " m")],
    ["フィルタ Y", num(fix.ky[index], 3, " m")],
    ["σ", mm(fix.ksig[index])],
    ["取り込み / 棄却", `${count(fix.kused[index])} / ${count(fix.krej[index])}`],
  ]);
}

// 表の距離の欄。値は生のまま出し、測距円にできない値 (負、高さの差より短い、タグの高さが不明) にはそう書き添える
function rangeCell(ranges, id, tagZ) {
  const r = ranges.get(id);
  if (!r) return ["--"];
  if (r.st !== 0) return [`失敗 (${r.st})`];
  const anchor = data.anchorById(id);
  const text = num(r.d, 3, " m");
  if (r.d !== null && r.d < 0) return [`${text} (負の値、円なし)`];
  if (!anchor) return [text];
  if (tagZ === null) return [`${text} (タグ高さ不明、円なし)`];
  if (horizontalRange(r.d, anchor.z - tagZ) === null) {
    return [`${text} (高さの差より短い、円なし)`];
  }
  return [text];
}

// フィルタの効果の表 (設計文書 8.4)。fix の from..to の範囲を、最小二乗とフィルタの同じサイクルどうしで比べる。
// rangeText は範囲の説明で、注記の先頭に出す。decimated が真なら跳びは出さない (設計文書 8.4)
function renderFilterMetrics(from, to, rangeText, decimated = false) {
  const m = filterMetrics(data.fix, from, to, { steps: !decimated });
  const body = $("metrics-body");
  if (!m) {
    body.replaceChildren();
    $("metrics-note").textContent = `${rangeText}: 最小二乗とフィルタがどちらも有効なサイクルがありません`;
    return;
  }
  const noSteps = decimated ? "-- (間引きあり)" : "--";
  const fmtMm = (v) => (v === null ? noSteps : mm(v, 1));
  const fmtCount = (v) => (v === null ? noSteps : String(v));
  const row = (label, lsValue, kfValue, format) => {
    const tr = document.createElement("tr");
    const r = ratio(kfValue, lsValue);
    const cells = [label, format(lsValue), format(kfValue), r === null ? "--" : r.toFixed(2)];
    cells.forEach((text, i) => {
      const td = document.createElement("td");
      td.textContent = text;
      // 比は 1 より小さければフィルタのほうが小さい (良い)。誤差程度の差では色を付けない
      if (i === 3 && r !== null) td.className = r < 0.9 ? "better" : r > 1.1 ? "worse" : "";
      tr.append(td);
    });
    return tr;
  };
  body.replaceChildren(
    row("散らばり RMS", m.ls.scatterRms, m.kf.scatterRms, fmtMm),
    row("跳び RMS", m.ls.stepRms, m.kf.stepRms, fmtMm),
    row("跳びの最大", m.ls.stepMax, m.kf.stepMax, fmtMm),
    row(`${JUMP_THRESHOLD_M * 1000} mm を超える跳び`, m.ls.jumps, m.kf.jumps, fmtCount),
  );
  $("metrics-note").textContent =
    `${rangeText}: 比較 ${m.compared} / ${m.cycles} サイクル、棄却した測距 ${m.rejected}、予測のみ ${m.predicted}。` +
    "散らばりは静止している区間で見る" +
    (decimated ? "。跳びは、グラフをドラッグして間引かれない範囲まで拡大すると出る" : "");
}

// ------------------------------------------------------------------ 描画

// XY 平面に描く軌跡の選択 (最小二乗 / フィルタ)
function trailToggles() {
  return { showLS: $("show-ls").checked, showFilter: $("show-filter").checked };
}

function refreshAnchors() {
  const ids = data.anchorIds();
  state.colors = anchorColors(ids);
  charts.setAnchors(ids, state.colors);
}

function render(now) {
  refreshAnchors();
  const fix = data.fix;
  const n = fix.t.length;
  if (state.mode === "live") {
    const last = data.lastT();
    const from = last === null ? 0 : lastIndexAtOrBefore(fix.t, last - TRAIL_MS - 1) + 1;
    xy.render({
      data,
      colors: state.colors,
      fromIndex: from,
      toIndex: n - 1,
      cursorIndex: n - 1,
      ended: !data.info.active,
      fitTrail: $("fit-trail").checked,
      ...trailToggles(),
    });
    // 受信し始めてから 30 秒たつまでは、横軸を最初のサイクルから始める
    const first = data.firstT();
    charts.update(data, last === null ? null : Math.max(first, last - TRAIL_MS), last);
    charts.setCursor(null);
    state.summaryPending = true;
    renderLiveSummaryThrottled(now);
    return;
  }

  const replay = state.replay;
  if (!replay) {
    // 読み込み中 (または失敗)。空のデータで描き、前の表示を残さない
    xy.render({
      data,
      colors: state.colors,
      fromIndex: 0,
      toIndex: -1,
      cursorIndex: -1,
      ended: true,
      fitTrail: false,
      ...trailToggles(),
    });
    charts.update(data, null, null);
    charts.setCursor(null);
    return;
  }
  const cursor = data.indexNear(replay.cursorT);
  const from = lastIndexAtOrBefore(fix.t, replay.windowMin - 1) + 1;
  xy.render({
    data,
    colors: state.colors,
    fromIndex: from,
    toIndex: cursor,
    cursorIndex: cursor,
    ended: true,
    fitTrail: $("fit-trail").checked,
    ...trailToggles(),
  });
  if (replay.chartsDirty) {
    charts.update(data, replay.windowMin, replay.windowMax);
    replay.chartsDirty = false;
    const to = lastIndexAtOrBefore(fix.t, replay.windowMax);
    const span = (replay.windowMax - replay.windowMin) / 1000;
    const decimated = (replay.decimation?.track.stride ?? 1) > 1;
    renderFilterMetrics(from, to, `表示範囲 ${span.toFixed(1)} 秒`, decimated);
  }
  charts.setCursor(replay.cursorT);
  renderReplayAnchors();
  renderCursorInfo(cursor);
  updateSliderLabel();
}

function frameLoop(now) {
  if (state.mode === "replay" && state.replay?.playing) advancePlayback(now);
  const paused = state.mode === "live" && state.live.paused;
  try {
    if (state.dirty && !paused) {
      state.dirty = false;
      render(now);
    } else if (state.mode === "live" && !paused) {
      // 間引いて描き残したサマリを、次のデータを待たずに描く
      renderLiveSummaryThrottled(now);
    }
  } catch (error) {
    showMessage(`描画に失敗しました: ${error.message}`);
    console.error(error);
  }
  requestAnimationFrame(frameLoop);
}

// ------------------------------------------------------------------ ライブ

// サマリの表は DOM を作り直すので、append ごと (50 ms) ではなく SUMMARY_INTERVAL_MS ごとに描く
function renderLiveSummaryThrottled(now) {
  if (!state.summaryPending || now - state.lastSummaryAt < SUMMARY_INTERVAL_MS) return;
  state.summaryPending = false;
  state.lastSummaryAt = now;
  renderLiveSummary();
  renderCursorInfo(data.fix.t.length - 1);
}

// 座標表の表示。サーバーはセッションの構成リビジョンがわからないか、この DB に無い場合に現在の構成を返すので、
// セッションのリビジョンと実際に使った座標表のリビジョンが違えばそれとわかるように書く
function configLabel(sessionRev, anchorsRev) {
  if (anchorsRev === null || anchorsRev === undefined) return "--";
  if (sessionRev === null || sessionRev === undefined) return `rev ${anchorsRev} (現在の構成)`;
  if (sessionRev !== anchorsRev) return `rev ${anchorsRev} (現在の構成。rev ${sessionRev} が無い)`;
  return `rev ${anchorsRev}`;
}

function renderLiveSummary() {
  const info = data.info;
  const ind = liveIndicators(data);
  $("summary-title").textContent = `ライブ: タグ ${info.tag_id ?? state.live.tagId}`;
  renderStats($("summary"), [
    ["セッション", info.session_id === null ? "--" : `#${info.session_id}`],
    ["boot_id", hex(info.boot_id, 8)],
    ["座標表", configLabel(info.config_rev, data.anchorsRev), info.config_rev !== null && info.config_rev !== data.anchorsRev],
    ["状態", info.boot_id === null ? "受信待ち" : info.active ? "受信中" : "終了", !info.active && info.boot_id !== null],
    ["測位レート (1 秒)", ind ? num(ind.fixRateHz, 1, " Hz") : "--"],
    ["サイクル (1 秒)", ind ? num(ind.cycleRateHz, 1, " Hz") : "--"],
    // 送信キューで捨てた append の穴を UDP の欠測と取り違えないよう、取り直すまでは欠測率を出さない
    state.live.resyncPending
      ? ["欠測率 (1 秒)", "-- (再同期中)"]
      : ["欠測率 (1 秒)", ind ? pct(ind.lossRate) : "--", ind && ind.missing > 0],
    ["連続失敗", ind ? String(ind.consecutiveFailures) : "--", ind && ind.consecutiveFailures > 0],
    ["フィルタ有効率 (1 秒)", ind ? pct(ind.kfRate) : "--"],
    ["予測のみ (1 秒)", ind ? String(ind.kfPredicted) : "--"],
    ["初期化 (1 秒)", ind ? String(ind.kfInit) : "--"],
    ["棄却した測距 (1 秒)", ind ? String(ind.kfRejected) : "--"],
    ["σ 平均 (1 秒)", ind ? mm(ind.kfSigmaMean, 1) : "--"],
    ["間引き (フレーム)", String(state.live.lostTotal), state.live.lostTotal > 0],
  ]);
  const n = data.fix.t.length;
  const windowMs = Number($("metrics-window").value);
  const last = data.lastT();
  const metricsFrom = last === null ? 0 : lastIndexAtOrBefore(data.fix.t, last - windowMs) + 1;
  renderFilterMetrics(metricsFrom, n - 1, `直近 ${windowMs / 1000} 秒`);
  const ranges = data.rangesAtIndex(n - 1);
  const tagZ = tagHeightAt(data, n - 1);
  renderAnchorTable(
    ["アンカー", "直近 1 秒", "距離"],
    data.anchorIds().map((id) => {
      const a = ind?.anchors.get(id);
      const rate = a ? a.rate : null;
      return { id, cells: [[pct(rate), lampClass(rate)], rangeCell(ranges, id, tagZ)] };
    }),
  );
}

function updateLiveStatus() {
  const parts = [];
  if (state.live.paused) parts.push("一時停止中 (受信は継続)");
  if (state.live.endReason) parts.push(END_REASONS[state.live.endReason] ?? state.live.endReason);
  if (state.live.lastFrameAt !== null) {
    parts.push(`最終受信 ${((performance.now() - state.live.lastFrameAt) / 1000).toFixed(1)} 秒前`);
  }
  $("live-status").textContent = parts.join(" / ");
}

function onFrame(frame) {
  if (state.mode !== "live") return;
  // append 以外のセッションの制御フレームにも lost が載る。append がすべて捨てられた場合もここで気づく
  if (frame.type !== "snapshot" && typeof frame.lost === "number") noteLost(frame.lost);
  switch (frame.type) {
    case "snapshot": {
      const previousBoot = data.info.boot_id;
      data.clear();
      data.setInfo(frame);
      data.setAnchors(frame.anchors, frame.anchors_rev);
      data.append(frame.fix, frame.ranges);
      // サーバーは購読し直すと lost を 0 から数え直す。取り直した snapshot で穴も埋まる
      state.live.lost = 0;
      state.live.resyncPending = false;
      // 最終受信は測定値が届いた時刻だけで数える。snapshot の中身は過去のデータのことがある
      state.live.lastFrameAt = null;
      // 取り直しの snapshot で同じ終了済みセッションを受けた場合は、先に受けた終了理由を残す
      if (frame.active || frame.boot_id === null) {
        state.live.endReason = null;
      } else if (!(state.live.endReason && frame.boot_id === previousBoot)) {
        state.live.endReason = "ended";
      }
      break;
    }
    case "session_start":
      // 同じ起動のまま戻ってきた場合 (途切れたあとの再開) は軌跡を残す
      if (frame.boot_id !== data.info.boot_id) data.clear();
      data.setInfo({ ...frame, active: true });
      data.setAnchors(frame.anchors, frame.anchors_rev);
      state.live.endReason = null;
      break;
    case "append":
      if (data.info.boot_id !== null && frame.boot_id !== data.info.boot_id) return;
      if (frame.session_id !== null) data.info.session_id = frame.session_id;
      data.append(frame.fix, frame.ranges);
      if (data.lastT() !== null) data.trimBefore(data.lastT() - TRAIL_MS);
      if (frame.fix.t.length > 0) state.live.lastFrameAt = performance.now();
      break;
    case "session_end":
      if (frame.boot_id === data.info.boot_id) {
        data.info.active = false;
        state.live.endReason = frame.reason;
      }
      break;
    case "session_info":
      // 終了済みのセッションの ID や構成リビジョンが、コミットや hello で後からわかった
      // 構成リビジョンが確定したら、その座標で最後の軌跡と測距円を描き直す
      if (frame.boot_id === data.info.boot_id) {
        if (frame.session_id !== null) data.info.session_id = frame.session_id;
        if (frame.config_rev !== null) data.info.config_rev = frame.config_rev;
        if (frame.anchors_rev !== null && frame.anchors_rev !== undefined) {
          data.setAnchors(frame.anchors, frame.anchors_rev);
        }
        state.summaryPending = true;
      }
      break;
    case "error":
      showMessage(`サーバー: ${frame.detail}`);
      return;
    default:
      return;
  }
  state.dirty = true;
}

// append の lost が増えたら、その間の append が届いていない。継ぎ足したデータには穴があるので、
// snapshot を取り直して埋める。遅いクライアントで取り直しが続かないよう、間隔は RESYNC_INTERVAL_MS 以上空ける
function noteLost(lost) {
  const added = lost - state.live.lost;
  state.live.lost = lost;
  if (added <= 0) return;
  state.live.lostTotal += added;
  state.live.resyncPending = true;
  requestResync();
}

function requestResync() {
  if (state.live.resyncTimer !== null) return;
  const wait = Math.max(0, state.live.lastResyncAt + RESYNC_INTERVAL_MS - performance.now());
  state.live.resyncTimer = setTimeout(() => {
    state.live.resyncTimer = null;
    if (state.mode !== "live" || !state.live.resyncPending) return;
    state.live.lastResyncAt = performance.now();
    live.subscribe(state.live.tagId, TRAIL_MS);
  }, wait);
}

function startLive(tagId) {
  stopPlayback();
  state.mode = "live";
  state.replay = null;
  // 読み込み途中の再生データが後から届いても、ライブの表示を上書きさせない
  state.replayToken = null;
  state.replaySessionId = null;
  state.live.tagId = tagId;
  state.live.endReason = null;
  state.live.lastFrameAt = null;
  state.live.lost = 0;
  state.live.lostTotal = 0;
  state.live.resyncPending = false;
  data.clear();
  data.setAnchors([], null);
  $("tag-field").hidden = false;
  $("metrics-window-field").hidden = false;
  $("live-controls").hidden = false;
  $("replay-controls").hidden = true;
  $("source").value = "live";
  history.replaceState(null, "", `#tag=${tagId}`);
  if (live.closed) live.open();
  live.subscribe(tagId, TRAIL_MS);
  state.dirty = true;
}

// ------------------------------------------------------------------ 再生

function renderReplaySummary(summary) {
  const s = summary.session;
  $("summary-title").textContent = `再生: セッション #${s.id}`;
  const decimation = state.replay?.decimation;
  renderStats($("summary"), [
    ["タグ", String(s.tag_id)],
    ["boot_id", hex(s.boot_id, 8)],
    ["FW", s.fw_version ?? "--"],
    ["座標表", configLabel(s.config_rev, summary.anchors_rev), s.config_rev !== null && s.config_rev !== summary.anchors_rev],
    ["開始", s.started_at.replace("T", " ").slice(0, 19)],
    ["サイクル数", String(s.cycles)],
    ["測位成功率", pct(s.fix_rate)],
    ["欠測率", `${pct(s.loss_rate)} (${s.missing_cycles})`, s.missing_cycles > 0],
    ["残差 RMS", num(summary.residual_rms === null ? null : summary.residual_rms * 1000, 1, " mm")],
    ["平均周期", num(summary.period_mean_ms, 1, " ms")],
    ["最大周期", num(summary.period_max_ms, 0, " ms")],
    ["使用数", summary.used_min === null ? "--" : `${summary.used_min}..${summary.used_max}`],
    ["フィルタ有効率", pct(s.cycles > 0 ? summary.kf_ok_cycles / s.cycles : null)],
    ["予測のみの周期", String(summary.kf_predicted_cycles)],
    ["初期化回数", String(summary.kf_init_count)],
    ["棄却した測距", String(summary.kf_rejected_ranges)],
    ["σ 平均", mm(summary.kf_sigma_mean, 1)],
    ["間引き", decimation ? `1/${decimation.track.stride}` : "--", decimation && decimation.track.stride > 1],
  ]);
}

function renderReplayAnchors() {
  const summary = state.replay.summary;
  const byId = new Map(summary.ranges.map((r) => [r.id, r]));
  const cursor = data.indexNear(state.replay.cursorT);
  const ranges = data.rangesAtIndex(cursor);
  const tagZ = tagHeightAt(data, cursor);
  renderAnchorTable(
    ["アンカー", "成功率", "距離", "平均 / 最大 elapsed", "失敗の内訳"],
    data.anchorIds().map((id) => {
      const r = byId.get(id);
      const failures = r
        ? Object.entries(r.status_counts)
            .filter(([code]) => code !== "0")
            .map(([code, n]) => `${code}: ${n}`)
            .join(", ")
        : "";
      return {
        id,
        cells: [
          [pct(r?.success_rate ?? null), lampClass(r?.success_rate ?? null)],
          rangeCell(ranges, id, tagZ),
          [r ? `${num(r.elapsed_mean_ms, 1)} / ${num(r.elapsed_max_ms)} ms` : "--"],
          [failures || "なし"],
        ],
      };
    }),
  );
}

function applyWindowData(windowData) {
  const replay = state.replay;
  const s = replay.summary.session;
  data.clear();
  data.setInfo({ session_id: s.id, tag_id: s.tag_id, boot_id: s.boot_id, config_rev: s.config_rev, active: false });
  data.setAnchors(replay.summary.anchors, replay.summary.anchors_rev);
  data.append(windowData.track.fix, windowData.ranges.ranges);
  replay.decimation = { track: windowData.track.decimation, ranges: windowData.ranges.decimation };
  replay.chartsDirty = true;
  renderReplaySummary(replay.summary);
}

function isDecimated(windowData) {
  if (windowData.track.decimation.stride > 1) return true;
  return Object.values(windowData.ranges.decimation).some((d) => d.stride > 1);
}

async function openReplay(sessionId) {
  stopPlayback();
  state.mode = "replay";
  live.close();
  onStatus("", "再生");
  $("tag-field").hidden = true;
  $("metrics-window-field").hidden = true;
  $("live-controls").hidden = true;
  $("replay-controls").hidden = false;
  history.replaceState(null, "", `#session=${sessionId}`);
  const token = Symbol("replay");
  state.replayToken = token;
  state.replaySessionId = sessionId;
  showMessage("");
  // 読み込みが終わるまで (失敗したときも)、直前のライブや別セッションの表示を選んだセッションとして見せない
  state.replay = null;
  data.clear();
  data.setAnchors([], null);
  $("summary-title").textContent = `再生: セッション #${sessionId} (読み込み中)`;
  for (const id of ["summary", "cursor-info", "anchor-head", "anchor-body", "metrics-body"]) $(id).replaceChildren();
  $("metrics-note").textContent = "";
  $("slider-label").textContent = "";
  state.dirty = true;
  try {
    const [summary, overview] = await Promise.all([loadSummary(sessionId), loadWindow(sessionId)]);
    if (state.replayToken !== token || state.mode !== "replay") return;
    // 全体の範囲は、描くデータと同じ track の応答の両端から取る。track は間引いても先頭と末尾の行を含む。
    // summary は別のリクエストなので、書き込み中のセッションでは track と違う時点の DB を見ていることがある
    const first = overview.track.fix.t[0] ?? summary.session.first_t_ms ?? 0;
    const last = overview.track.fix.t.at(-1) ?? summary.session.last_t_ms ?? 0;
    state.replay = {
      sessionId,
      summary,
      overview,
      decimation: null,
      fullMin: first,
      fullMax: last,
      windowMin: first,
      windowMax: last,
      cursorT: last,
      playing: false,
      lastTs: null,
      chartsDirty: true,
      zoomTimer: null,
      zoomToken: 0,
    };
    applyWindowData(overview);
    updateSlider();
    state.dirty = true;
  } catch (error) {
    if (state.replayToken !== token) return;
    $("summary-title").textContent = `再生: セッション #${sessionId} (読み込み失敗)`;
    showMessage(`セッションを読めませんでした: ${error.message}`);
  }
}

function updateSlider() {
  const replay = state.replay;
  const slider = $("slider");
  slider.min = String(replay.windowMin);
  slider.max = String(replay.windowMax);
  slider.value = String(replay.cursorT);
  updateSliderLabel();
}

function updateSliderLabel() {
  const replay = state.replay;
  if (!replay) return;
  const offset = (replay.cursorT - replay.fullMin) / 1000;
  $("slider-label").textContent = `${num(replay.cursorT / 1000, 3)} s (開始から +${num(offset, 1)} s)`;
}

function setCursor(t) {
  const replay = state.replay;
  replay.cursorT = Math.min(replay.windowMax, Math.max(replay.windowMin, t));
  $("slider").value = String(replay.cursorT);
  state.dirty = true;
}

function onPick(t) {
  if (state.mode === "replay" && state.replay) setCursor(t);
}

// グラフの範囲選択で表示範囲を絞る。間引かれていれば、その範囲だけを細かく取り直す
function onZoom(min, max) {
  const replay = state.replay;
  if (state.mode !== "replay" || !replay) {
    state.dirty = true;
    return;
  }
  replay.windowMin = min;
  replay.windowMax = max;
  replay.chartsDirty = true;
  setCursor(replay.cursorT);
  updateSlider();
  clearTimeout(replay.zoomTimer);
  if (!isDecimated(replay.overview)) return;
  const token = ++replay.zoomToken;
  replay.zoomTimer = setTimeout(async () => {
    try {
      const detail = await loadWindow(replay.sessionId, min, max);
      if (state.replay !== replay || replay.zoomToken !== token) return;
      applyWindowData(detail);
      state.dirty = true;
    } catch (error) {
      showMessage(`区間を読めませんでした: ${error.message}`);
    }
  }, ZOOM_FETCH_DELAY_MS);
}

function onReset() {
  const replay = state.replay;
  if (state.mode !== "replay" || !replay) return;
  clearTimeout(replay.zoomTimer);
  replay.zoomToken++;
  replay.windowMin = replay.fullMin;
  replay.windowMax = replay.fullMax;
  applyWindowData(replay.overview);
  setCursor(replay.cursorT);
  updateSlider();
}

function advancePlayback(now) {
  const replay = state.replay;
  if (replay.lastTs !== null) {
    const speed = Number($("speed").value);
    const next = replay.cursorT + (now - replay.lastTs) * speed;
    if (next >= replay.windowMax) {
      setCursor(replay.windowMax);
      stopPlayback();
      return;
    }
    setCursor(next);
  }
  replay.lastTs = now;
}

function stopPlayback() {
  if (state.replay) {
    state.replay.playing = false;
    state.replay.lastTs = null;
  }
  $("play").textContent = "再生";
  $("play").setAttribute("aria-pressed", "false");
}

function togglePlayback() {
  const replay = state.replay;
  if (!replay) return;
  if (replay.playing) {
    stopPlayback();
    return;
  }
  if (replay.cursorT >= replay.windowMax) setCursor(replay.windowMin);
  replay.playing = true;
  replay.lastTs = null;
  $("play").textContent = "停止";
  $("play").setAttribute("aria-pressed", "true");
}

// ------------------------------------------------------------------ セッション一覧と起動

async function reloadSessions() {
  const select = $("source");
  try {
    const sessions = await loadSessions();
    // 選ぶ値は取得が終わった時点の表示状態から決める。取得中にライブ / 再生を切り替えた場合に、
    // 取得前の選択を書き戻して表示と実際のデータを食い違わせないため
    const current = state.mode === "replay" && state.replaySessionId !== null ? String(state.replaySessionId) : "live";
    const options = [new Option("ライブ", "live")];
    for (const s of sessions) {
      const started = s.started_at.replace("T", " ").slice(0, 19);
      options.push(new Option(`#${s.id} タグ ${s.tag_id} ${started} (${s.cycles} サイクル)`, String(s.id)));
    }
    select.replaceChildren(...options);
    if (current !== "live") {
      // 再生中のセッションが最新の一覧から外れても、表示と選択肢を食い違わせない
      selectSessionOption(current);
    } else {
      select.value = "live";
    }
  } catch (error) {
    showMessage(`セッション一覧を読めませんでした: ${error.message}`);
  }
}

// セッションを選択肢で選んだ状態にする。一覧 (最新 100 件) に無ければ選択肢を足す
function selectSessionOption(id) {
  const select = $("source");
  const value = String(id);
  if (![...select.options].some((o) => o.value === value)) {
    select.append(new Option(`#${value} (一覧の範囲外)`, value));
  }
  select.value = value;
}

function tagIdFromInput() {
  const value = Number($("tag-id").value);
  return Number.isInteger(value) && value >= 1 && value <= 255 ? value : null;
}

$("source").addEventListener("change", (event) => {
  const value = event.target.value;
  if (value === "live") {
    startLive(tagIdFromInput() ?? 1);
  } else {
    openReplay(Number(value));
  }
});

$("tag-id").addEventListener("change", () => {
  const tagId = tagIdFromInput();
  if (tagId === null) {
    showMessage("タグ ID は 1..255 です");
    return;
  }
  showMessage("");
  if (state.mode === "live") startLive(tagId);
});

$("reload-sessions").addEventListener("click", reloadSessions);

$("pause").addEventListener("click", () => {
  // 一時停止中も受信は続け、描画だけを止める。再開すると最新の状態へ追いつく (早送りはしない)
  state.live.paused = !state.live.paused;
  $("pause").textContent = state.live.paused ? "再開" : "一時停止";
  $("pause").setAttribute("aria-pressed", String(state.live.paused));
  state.dirty = true;
  updateLiveStatus();
});

$("metrics-window").addEventListener("change", () => {
  state.summaryPending = true;
});

for (const id of ["fit-trail", "show-ls", "show-filter"]) {
  $(id).addEventListener("change", () => {
    state.dirty = true;
  });
}

$("slider").addEventListener("input", (event) => {
  if (!state.replay) return;
  stopPlayback();
  setCursor(Number(event.target.value));
});

$("play").addEventListener("click", togglePlayback);
$("zoom-reset").addEventListener("click", onReset);

new ResizeObserver(() => {
  state.dirty = true;
}).observe($("xy"));

setInterval(() => {
  if (state.mode === "live") updateLiveStatus();
}, 500);

function startFromHash() {
  const match = /^#(tag|session)=(\d+)$/.exec(location.hash);
  if (match && match[1] === "session") {
    selectSessionOption(match[2]);
    openReplay(Number(match[2]));
    return;
  }
  // URL から読んだタグ ID にも入力欄と同じ範囲 (1..255) を課し、外れていれば既定の 1 に戻す
  const fromHash = match ? Number(match[2]) : null;
  const tagId = fromHash !== null && fromHash >= 1 && fromHash <= 255 ? fromHash : 1;
  if (fromHash !== null && tagId !== fromHash) showMessage(`タグ ID は 1..255 です (${fromHash} を 1 に戻しました)`);
  $("tag-id").value = String(tagId);
  startLive(tagId);
}

reloadSessions().then(startFromHash);
requestAnimationFrame(frameLoop);
