// データの供給元。ライブは WebSocket (設計文書 5.6)、再生は参照 API (5.5) から取る。
// どちらもサーバーが返す列指向の形のまま呼び出し側へ渡し、描画側の処理は共通にする。

const RECONNECT_MS = 2000;

export class LiveSource {
  // handlers: { onFrame(frame), onStatus(state, text) }
  constructor(handlers) {
    this.handlers = handlers;
    this.ws = null;
    this.subscription = null;
    this.closed = true;
    this._timer = null;
  }

  open() {
    this.closed = false;
    this._connect();
  }

  close() {
    this.closed = true;
    clearTimeout(this._timer);
    if (this.ws) this.ws.close();
    this.ws = null;
  }

  subscribe(tagId, historyMs = 30000) {
    this.subscription = { op: "subscribe", tag_id: tagId, history_ms: historyMs };
    this._send(this.subscription);
  }

  _send(message) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(message));
  }

  _connect() {
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${scheme}//${location.host}/api/v1/ws/live`);
    this.ws = ws;
    this.handlers.onStatus("warn", "接続中");
    ws.addEventListener("open", () => {
      this.handlers.onStatus("ok", "接続");
      // 再接続したときも購読をやり直す。サーバーは snapshot から送り直す
      if (this.subscription) this._send(this.subscription);
    });
    ws.addEventListener("message", (event) => {
      let frame;
      try {
        frame = JSON.parse(event.data);
      } catch {
        return;
      }
      this.handlers.onFrame(frame);
    });
    ws.addEventListener("close", () => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.closed) return;
      this.handlers.onStatus("bad", "切断 (再接続待ち)");
      this._timer = setTimeout(() => this._connect(), RECONNECT_MS);
    });
  }
}

async function fetchJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      // 本文が JSON でなければ statusText のままにする
    }
    throw new Error(`${url}: ${response.status} ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }
  return response.json();
}

export function loadSessions(limit = 100) {
  return fetchJSON(`/api/v1/sessions?limit=${limit}`).then((body) => body.sessions);
}

export function loadSummary(sessionId) {
  return fetchJSON(`/api/v1/sessions/${sessionId}/summary`);
}

// 軌跡と測距をまとめて取る。fromMs / toMs はタグの millis() で、null なら端を切らない
export async function loadWindow(sessionId, fromMs = null, toMs = null, maxPoints = 2000) {
  const params = new URLSearchParams({ max_points: String(maxPoints) });
  if (fromMs !== null) params.set("from_ms", String(Math.max(0, Math.floor(fromMs))));
  if (toMs !== null) params.set("to_ms", String(Math.max(0, Math.ceil(toMs))));
  const [track, ranges] = await Promise.all([
    fetchJSON(`/api/v1/sessions/${sessionId}/track?${params}`),
    fetchJSON(`/api/v1/sessions/${sessionId}/ranges?${params}`),
  ]);
  return { track, ranges };
}
