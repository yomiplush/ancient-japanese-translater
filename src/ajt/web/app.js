/* Live capture loop + result rendering.
 *
 * Design notes:
 * - One frame in flight at a time. OCR takes ~1s on CPU, so pipelining frames
 *   would just queue work behind a stale image.
 * - The send interval adapts to measured latency, so a slow machine degrades
 *   to a lower frame rate instead of falling further behind.
 * - The server decides whether a frame is redundant (FrameGate); a skipped
 *   response costs almost nothing, which is why we can poll cheaply.
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const els = {
    video: $("video"), shot: $("shot"), overlay: $("overlay"),
    placeholder: $("placeholder"), status: $("statusPill"), toast: $("toast"),
    modern: $("modern"), era: $("era"), source: $("source"),
    srcLen: $("srcLen"), glosses: $("glosses"), glossCount: $("glossCount"),
    lines: $("lines"), lineCount: $("lineCount"), backend: $("backendPill"),
    provider: $("providerLabel"), mOcr: $("mOcr"), mTotal: $("mTotal"),
    mConf: $("mConf"), mFrames: $("mFrames"),
    auto: $("autoToggle"), fileInput: $("fileInput"),
    pairUrl: $("pairUrl"), qrBox: $("qrBox"),
  };

  const state = {
    streaming: false,
    busy: false,
    token: new URLSearchParams(location.search).get("t") || "",
    timer: null,
    interval: 1200,
    ws: null,
    wsRetry: 0,
    lastLines: [],
    lastLayout: "vertical",
  };

  const MIN_INTERVAL = 550;
  const MAX_INTERVAL = 4000;
  // Must match AJT_OCR_MAX_SIDE: boxes come back in this coordinate space.
  const MEDIA_MAX_SIDE = 1280;

  function toast(msg, ms = 2400) {
    els.toast.textContent = msg;
    els.toast.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { els.toast.hidden = true; }, ms);
  }

  function setStatus(text, cls = "") {
    els.status.textContent = text;
    els.status.className = "pill " + cls;
  }

  async function fetchJSON(url, opts = {}) {
    const sep = url.includes("?") ? "&" : "?";
    const withToken = state.token ? url + sep + "t=" + encodeURIComponent(state.token) : url;
    const res = await fetch(withToken, opts);
    if (res.status === 401) throw new Error("認証エラー: トークンを確認してください");
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }

  /* ---------------------------------------------------------------- capture */
  const work = document.createElement("canvas");

  function grabFrame() {
    const v = els.video;
    if (!v.videoWidth) return null;
    const scale = Math.min(1, 1280 / Math.max(v.videoWidth, v.videoHeight));
    work.width = Math.round(v.videoWidth * scale);
    work.height = Math.round(v.videoHeight * scale);
    work.getContext("2d").drawImage(v, 0, 0, work.width, work.height);
    return new Promise((resolve) => work.toBlob(resolve, "image/jpeg", 0.85));
  }

  async function sendBlob(blob, { force = false } = {}) {
    const form = new FormData();
    form.append("file", blob, "frame.jpg");
    const url = `/api/frame?translate=1${force ? "&force=1" : ""}`;
    const started = performance.now();
    const data = await fetchJSON(url, { method: "POST", body: form });
    return { data, roundTrip: performance.now() - started };
  }

  async function tick() {
    if (!state.streaming || state.busy) return;
    const blob = await grabFrame();
    if (!blob) return;

    state.busy = true;
    setStatus("読み取り中…", "live");
    try {
      const { data, roundTrip } = await sendBlob(blob);
      if (data.skipped) {
        state.interval = Math.min(MAX_INTERVAL, state.interval + 150);
        updateStats(data.stats, null, 0);
        setStatus("同じ画面", "live");
      } else {
        render(data);
        const cost = data.total_ms || roundTrip;
        state.interval = Math.max(MIN_INTERVAL, Math.min(MAX_INTERVAL, cost * 0.9));
        setStatus("ライブ", "live");
      }
    } catch (err) {
      setStatus("エラー", "err");
      toast(err.message, 3000);
      state.interval = Math.min(MAX_INTERVAL, state.interval + 600);
    } finally {
      state.busy = false;
      schedule();
    }
  }

  function schedule() {
    clearTimeout(state.timer);
    if (!state.streaming) return;
    state.timer = setTimeout(tick, state.interval);
  }

  async function startCamera() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "environment", width: { ideal: 1920 }, height: { ideal: 1440 } },
        audio: false,
      });
      els.video.srcObject = stream;
      await els.video.play();
      els.video.hidden = false;
      els.shot.hidden = true;
      els.placeholder.hidden = true;
      state.streaming = true;
      state.interval = 1200;
      $("btnCamera").textContent = "カメラ停止";
      setStatus("ライブ", "live");
      schedule();
    } catch (err) {
      toast("カメラを開けません: " + err.message + "（HTTPSが必要な場合があります）", 5000);
      setStatus("カメラ不可", "err");
    }
  }

  function stopCamera() {
    state.streaming = false;
    clearTimeout(state.timer);
    const stream = els.video.srcObject;
    if (stream) stream.getTracks().forEach((t) => t.stop());
    els.video.srcObject = null;
    els.video.hidden = true;
    $("btnCamera").textContent = "カメラ開始";
    setStatus("停止", "");
  }

  /* ----------------------------------------------------------------- render */
  function render(data) {
    const tr = data.translation || {};
    const ocr = data.ocr || {};

    if (tr.modern) {
      els.modern.innerHTML = tr.modern
        .split(/\n+/).filter(Boolean)
        .map((p) => `<p>${escapeHtml(p)}</p>`).join("");
    } else if (ocr.empty) {
      els.modern.innerHTML = '<p class="muted">文字が見つかりませんでした。ピントと明るさを確認してください。</p>';
    }

    els.era.textContent = tr.era ? `推定時代: ${tr.era}` : "";
    els.backend.textContent = tr.backend || "—";
    els.backend.className = "pill " + (tr.backend === "offline" ? "" : "accent");

    const src = data.source || ocr.text || "";
    els.source.textContent = src || "—";
    els.srcLen.textContent = src.length;

    const glosses = tr.glosses || [];
    els.glossCount.textContent = glosses.length;
    els.glosses.innerHTML = glosses.length
      ? glosses.map((g) => `<li><b>${escapeHtml(g.term)}</b> — ${escapeHtml(g.meaning)}</li>`).join("")
      : '<li class="muted">—</li>';

    const lines = ocr.lines || [];
    els.lineCount.textContent = lines.length;
    els.lines.innerHTML = lines.length
      ? lines.map((l) => `<li>${escapeHtml(l.text)}<code>${l.score.toFixed(2)}</code></li>`).join("")
      : '<li class="muted">—</li>';

    drawBoxes(ocr.lines || [], ocr.layout);
    state.lastLines = ocr.lines || [];
    state.lastLayout = ocr.layout || "vertical";
    updateStats(data.stats, ocr, data.total_ms);

    if (tr.notes) els.status.title = tr.notes;
  }

  function updateStats(stats, ocr, totalMs) {
    if (stats) els.mFrames.textContent = stats.frames ?? 0;
    if (ocr) {
      els.mOcr.textContent = (ocr.elapsed_ms ?? 0) + "ms";
      els.mConf.textContent = ((ocr.mean_score ?? 0) * 100).toFixed(0) + "%";
    }
    if (totalMs) els.mTotal.textContent = totalMs + "ms";
  }

  /* Overlay column boxes so the user can see what the OCR actually read. */
  function drawBoxes(lines, layout) {
    const canvas = els.overlay;
    const media = els.video.hidden ? els.shot : els.video;
    if (!lines.length) {
      canvas.width = canvas.height = 0;
      return;
    }
    const rect = media.getBoundingClientRect();
    const view = els.overlay.getBoundingClientRect();
    if (!rect.width) {
      canvas.width = canvas.height = 0;
      return;
    }
    canvas.width = view.width * devicePixelRatio;
    canvas.height = view.height * devicePixelRatio;
    const ctx = canvas.getContext("2d");
    ctx.scale(devicePixelRatio, devicePixelRatio);
    ctx.clearRect(0, 0, view.width, view.height);

    const natW = media.videoWidth || media.naturalWidth;
    const natH = media.videoHeight || media.naturalHeight;
    if (!natW || !natH) return;

    // object-fit: contain leaves letterbox bars; map inside the fitted box.
    const fit = Math.min(rect.width / natW, rect.height / natH);
    const offX = rect.left - view.left + (rect.width - natW * fit) / 2;
    const offY = rect.top - view.top + (rect.height - natH * fit) / 2;

    // Boxes are in the server-resized image; scale them back to native px.
    const natLongest = Math.max(natW, natH);
    const serverLongest = Math.min(natLongest, MEDIA_MAX_SIDE);
    const back = natLongest / serverLongest;

    ctx.lineWidth = 1.25;
    for (const line of lines) {
      const [x0, y0, x1, y1] = line.box;
      const x = offX + x0 * back * fit;
      const y = offY + y0 * back * fit;
      const w = (x1 - x0) * back * fit;
      const h = (y1 - y0) * back * fit;
      ctx.strokeStyle = line.score > 0.7 ? "rgba(216,162,74,.85)" : "rgba(201,107,90,.7)";
      ctx.strokeRect(x, y, w, h);
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* -------------------------------------------------------------------- ws */
  function connectWS() {
    if (state.ws && state.ws.readyState <= 1) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws?t=${encodeURIComponent(state.token)}`;
    const ws = new WebSocket(url);
    state.ws = ws;
    ws.onopen = () => {
      state.wsRetry = 0;
      ws.send("ping");
    };
    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.pong) return;
        if (data.ocr) render(data);
      } catch (_) {}
    };
    ws.onclose = () => {
      if (state.wsRetry++ < 6) setTimeout(connectWS, 1000 * state.wsRetry);
    };
    ws.onerror = () => ws.close();
  }

  /* ------------------------------------------------------------------ boot */
  async function boot() {
    try {
      const health = await fetch("/api/health").then((r) => r.json());
      els.provider.textContent = health.provider;
      if (health.provider === "offline") {
        els.provider.textContent = "offline（ルールベース）";
      }
      if (health.capture_error) {
        setStatus("カメラ未接続", "err");
        els.status.title = health.capture_error;
        toast("IPカメラに接続できません: " + health.capture_error, 6000);
      } else if (health.source) {
        setStatus("IPカメラ待機", "live");
      }
    } catch (_) {}

    try {
      const info = await fetchJSON("/api/pair");
      els.qrBox.innerHTML = info.svg;
      els.pairUrl.textContent = info.url;
      els.pairUrl.title = info.url;
      state.token = state.token || info.token;
    } catch (err) {
      // ペアリング情報はPC上でのみ配信される。別端末からGUIを開いた場合は
      // トークンからカメラURLを組み立てて、QRの代わりにURLを見せる。
      if (state.token) {
        const url = `${location.origin}/camera?t=${encodeURIComponent(state.token)}`;
        els.pairUrl.textContent = url;
        els.pairUrl.title = url;
        els.qrBox.innerHTML = '<span class="qr-fallback">URLを<br>共有</span>';
      } else {
        els.pairUrl.textContent = err.message;
      }
    }

    connectWS();

    const params = new URLSearchParams(location.search);
    if (params.get("t")) {
      state.token = params.get("t");
      els.pairUrl.textContent = location.origin + "/camera?t=" + state.token;
    }
  }

  /* --------------------------------------------------------------- events */
  $("btnCamera").addEventListener("click", () => {
    if (state.streaming) stopCamera(); else startCamera();
  });

  $("btnFile").addEventListener("click", () => els.fileInput.click());

  els.fileInput.addEventListener("change", async () => {
    const file = els.fileInput.files && els.fileInput.files[0];
    if (!file) return;
    stopCamera();
    els.shot.src = URL.createObjectURL(file);
    els.shot.hidden = false;
    els.placeholder.hidden = true;
    setStatus("読み取り中…", "live");
    try {
      const { data } = await sendBlob(file, { force: true });
      render(data);
      setStatus("読み取り完了", "live");
    } catch (err) {
      setStatus("エラー", "err");
      toast(err.message);
    }
  });

  els.auto.addEventListener("change", () => {
    if (els.auto.checked) schedule(); else clearTimeout(state.timer);
  });

  $("btnCopy").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(els.pairUrl.textContent);
      toast("URLをコピーしました");
    } catch (_) { toast("コピーできませんでした"); }
  });

  $("btnCopySrc").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(els.source.textContent || "");
      toast("原文をコピーしました");
    } catch (_) { toast("コピーできませんでした"); }
  });

  $("btnEdit").addEventListener("click", async () => {
    const edited = prompt("原文を編集して再翻訳します", els.source.textContent || "");
    if (edited === null) return;
    try {
      const data = await fetchJSON("/api/text", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: edited }),
      });
      els.source.textContent = data.source || edited;
      render({ source: data.source, ocr: { lines: [], layout: "vertical" }, translation: data.translation });
      toast("再翻訳しました");
    } catch (err) { toast(err.message); }
  });

  window.addEventListener("resize", () => {
    if (state.lastLines.length) drawBoxes(state.lastLines, state.lastLayout);
  });

  boot();
})();
