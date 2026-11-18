"use strict";

// ------------------------------------------------------------------ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const api = async (path, opts) => {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.headers.get("content-type")?.includes("json") ? res.json() : res.text();
};
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
const yen = (n) => (n == null ? "—" : `${Number(n).toLocaleString("ja-JP")}円`);
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString("ja-JP", { dateStyle: "short", timeStyle: "short" }) : "");
const STATUS_ICON = { ok: "✓", missing: "○", needs_detail: "△", waived: "⏭" };
const JOB_STATUS = { open: "未報告", draft: "途中保存", reported: "報告済み" };
const COMPLETENESS = { complete: "全項目あり", complete_with_exemptions: "免除あり" };
const GROUP_JA = { material: "材料費", labor: "作業費", support: "諸経費・付帯費", adjustment: "調整費" };

// Same breakpoint as the phone rules at the end of style.css.
const PHONE = matchMedia("(max-width: 860px)");
// Every browser on iOS is WebKit (Chrome there says "CriOS", not "Chrome").
const IS_WEBKIT = /AppleWebKit/.test(navigator.userAgent) && !/Chrome|Chromium|Android/.test(navigator.userAgent);
// The mic and AudioWorklet exist only on https:// or localhost.
const micAvailable = () => window.isSecureContext && !!navigator.mediaDevices?.getUserMedia && "AudioWorkletNode" in window;

let CONFIG = {};

// ------------------------------------------------------------------ tabs
// URL hash: #worker, #worker/J-1001, #approver, #approver/<report_id>, #rules
function parseHash() {
  const [view, id] = location.hash.slice(1).split("/");
  return [["worker", "approver", "rules"].includes(view) ? view : "worker", id && decodeURIComponent(id)];
}

function showView(view, id) {
  $$(".tab").forEach((x) => x.classList.toggle("active", x.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("hidden", v.id !== `view-${view}`));
  // Phone: a tab (or Back) without an id shows that tab's list. A running call keeps its screen.
  const inCall = view === "worker" && call;
  if (!id && !inCall) $(`#view-${view}`).classList.remove("has-detail");
  history.replaceState(null, "", `#${view}${id ? "/" + id : inCall ? "/" + call.job.job_id : ""}`);
  if (view === "approver") return loadReports().then(() => id && openReport(id));
  if (view === "rules") return loadRules();
  return loadJobs(id);
}
$$(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));

// ------------------------------------------------------------------ phone: list ⇄ detail
// On a phone the list and the selected item are separate screens (CSS: .has-detail).
// Opening an item from the list adds a history entry, so the system Back gesture returns to the list.
let detailPushed = false;

function pushDetail() {
  if (!PHONE.matches) return;
  history.pushState(null, "", location.hash);
  detailPushed = true;
}

function showDetail(view) {
  $(`#view-${view}`).classList.add("has-detail");
  if (PHONE.matches) scrollTo(0, 0);
}

function backToList(view) {
  if (detailPushed) history.back(); // → popstate
  else showView(view);
}

addEventListener("popstate", () => {
  detailPushed = false;
  if (call) { // the call keeps its screen; 終了 is the way out
    history.pushState(null, "", `#worker/${call.job.job_id}`);
    detailPushed = true;
    call.sysline("通話中です。戻るには「終了」で通話を終えてください。");
    return;
  }
  showView(...parseHash());
});

// ------------------------------------------------------------------ worker
let selectedJob = null;
let call = null; // the running Call, if any

async function loadJobs(wantId) {
  const jobs = await api("/api/jobs");
  const wanted = wantId && jobs.find((j) => j.job_id === wantId);
  if (wanted && !call) selectedJob = null;
  const box = $("#jobs");
  box.replaceChildren(
    ...jobs.map((j) =>
      el("button", {
        class: `jobcard ${selectedJob?.job_id === j.job_id ? "active" : ""}`,
        onclick: () => { if (!call) { pushDetail(); selectJob(j); } },
      },
        el("div", { class: "top" }, el("span", {}, `${j.large}／${j.small}`), el("span", { class: `badge ${j.status}` }, JOB_STATUS[j.status])),
        el("div", { class: "name" }, j.store_name),
        el("div", { class: "snippet" }, j.description))
    )
  );
  // A phone starts on the list; a desktop opens the first open job next to it.
  if (!selectedJob && jobs.length && (wanted || !PHONE.matches)) {
    await selectJob(wanted || jobs.find((j) => j.status !== "reported") || jobs[0]);
  }
  return jobs;
}

function micHelp() {
  return [
    el("strong", {}, "このままではマイクを使えません。"),
    " マイクは https:// のページでしか使えません。管理者が ", el("code", {}, "scripts/serve_lan.py"),
    " で起動したときに表示される https:// のアドレスを開いてください。",
    el("div", { class: "vi" }, "Trình duyệt chỉ cho dùng micro trên https:// hoặc localhost. Chạy server bằng scripts/serve_lan.py rồi mở địa chỉ https:// nó in ra."),
  ];
}

// Phone only: the transcript and the checklist share the screen as two tabs.
function setPaneTab(pane, which) {
  $(".grid2", pane).dataset.show = which;
  $$(".panetab", pane).forEach((b) => b.classList.toggle("active", b.dataset.show === which));
  if (which === "checklist") $(".panetab[data-show=checklist]", pane).classList.remove("has-update");
  if (!PHONE.matches) return;
  const root = document.scrollingElement;
  if (which === "transcript" && call?.pane === pane) { root.scrollTop = root.scrollHeight; return; } // newest line
  // Otherwise start the other tab at its top, not mid-way down where the previous one was.
  const top = $(".grid2", pane).getBoundingClientRect().top + scrollY - $(".panetabs", pane).offsetHeight;
  if (scrollY > top) scrollTo(0, top);
}

async function selectJob(job) {
  selectedJob = job;
  history.replaceState(null, "", `#worker/${job.job_id}`);
  $$("#jobs .jobcard").forEach((c) => c.classList.toggle("active", c.querySelector(".name").textContent === job.store_name));
  const pane = $("#jobPane");
  pane.replaceChildren($("#tpl-call").content.cloneNode(true));
  pane.classList.remove("calling");
  showDetail("worker");
  $(".back", pane).onclick = () => backToList("worker");
  $$(".panetab", pane).forEach((b) => (b.onclick = () => setPaneTab(pane, b.dataset.show)));
  setPaneTab(pane, "checklist");
  if (!micAvailable()) {
    $(".micwarn", pane).classList.remove("hidden");
    $(".micwarn", pane).replaceChildren(...micHelp());
  }
  $(".issue", pane).textContent = `${job.large}／${job.small}　#${job.trouble_number}`;
  $(".store", pane).textContent = job.store_name;
  $(".meta", pane).textContent = `訪問日 ${job.visit_date}　作業者 ${job.worker.name}さん　依頼 ${fmtDate(job.requested_at)}`;
  $(".desc", pane).textContent = `店舗からの依頼：${job.description}`;
  $(".maxmin", pane).textContent = Math.round((CONFIG.max_session_seconds || 480) / 60);

  const preview = await api(`/api/jobs/${job.job_id}/checklist`);
  // Every field's label, conditional ones included, for the call's tool log.
  pane.fieldLabels = Object.fromEntries(preview.fields.map((f) => [f.key, f.label]));
  renderState(pane, preview.state, false);
  $(".resume", pane).classList.toggle("hidden", !preview.resumed);
  $(".discard", pane).onclick = async () => {
    if (!confirm("途中保存の内容を破棄しますか？")) return;
    await api(`/api/jobs/${job.job_id}/draft`, { method: "DELETE" });
    await loadJobs();
    selectJob({ ...job, status: "open" });
  };

  if (job.status === "reported") {
    $(".precall", pane).classList.add("hidden");
    $(".grid2", pane).classList.add("hidden"); // the report itself is on the approver screen
    $(".panetabs", pane).classList.add("hidden");
    showDone(pane, job.report_id);
    return;
  }
  $(".start", pane).onclick = () => startCall(pane, job, $(".lang", pane).value);
}

function renderState(pane, state, flash) {
  const cl = state.checklist;
  $(".progress span", pane).style.width = `${cl.total ? (100 * (cl.done + cl.waived.length)) / cl.total : 0}%`;
  $(".progress-text", pane).textContent = cl.status === "ready" ? "✓ すべて揃いました" : `${cl.done + cl.waived.length} / ${cl.total}`;
  $(".pt-count", pane).textContent = `${cl.done + cl.waived.length}/${cl.total}`;

  const list = $(".checklist", pane);
  const before = new Map($$("li", list).map((li) => [li.dataset.key, li.dataset.sig]));
  let changed = false;
  list.replaceChildren(
    ...state.fields.map((f) => {
      const sig = `${f.status}|${f.value}`;
      const li = el("li", { class: `st-${f.status}`, "data-key": f.key, "data-sig": sig },
        el("span", { class: "icon" }, STATUS_ICON[f.status] || "•"),
        el("div", {},
          el("div", { class: "row" }, el("span", { class: "lbl" }, f.label),
            f.source !== "共通" && el("span", { class: "badge src" }, f.source),
            !f.waivable && f.status !== "ok" && el("span", { class: "badge src" }, "免除不可")),
          f.value && el("div", { class: "val" }, f.value),
          f.status === "waived" && f.exemption && el("div", { class: "issue" }, `免除：${f.exemption.reason}${f.exemption.reason_is_specific ? "" : "（理由が具体的でない）"}`),
          f.status !== "ok" && f.status !== "waived" && f.issue && f.issue !== "未入力" && el("div", { class: "issue" }, f.issue),
          f.status === "missing" && f.issue === "未入力" && el("div", { class: "issue" }, "未入力"))
      );
      if (flash && before.has(f.key) && before.get(f.key) !== sig) { li.classList.add("flash"); changed = true; }
      return li;
    })
  );
  // Phone: a dot on the 報告書 tab while the worker is looking at the conversation.
  if (changed && $(".grid2", pane).dataset.show !== "checklist") $(".panetab[data-show=checklist]", pane).classList.add("has-update");

  const costs = $(".costs", pane);
  const rows = state.cost_items.map((i) =>
    el("tr", {}, el("td", {}, i.label, i.note && el("div", { class: "muted small" }, i.note)),
      el("td", {}, GROUP_JA[i.cost_group] || i.cost_group),
      el("td", { class: "num" }, i.quantity), el("td", { class: "num" }, yen(i.amount))));
  const open = state.cost_requirements.filter((r) => r.status !== "ok");
  costs.replaceChildren(
    state.cost_items.length
      ? el("div", { class: "tablewrap" }, el("table", {},
          el("thead", {}, el("tr", {}, el("th", {}, "項目"), el("th", {}, "区分"), el("th", { class: "num" }, "数量"), el("th", { class: "num" }, "金額"))),
          el("tbody", {}, rows),
          el("tfoot", {}, el("tr", {}, el("th", { colspan: 3 }, "合計（税抜）"), el("th", { class: "num" }, yen(state.cost_total))))))
      : el("div", { class: "muted small" }, "未入力"),
    el("ul", { class: "checklist" },
      open.map((r) => el("li", { class: `st-${r.status}` },
        el("span", { class: "icon" }, STATUS_ICON[r.status]),
        el("div", {}, el("div", { class: "lbl" }, r.label),
          el("div", { class: "issue" }, r.status === "waived" && r.exemption ? `免除：${r.exemption.reason}` : r.issue)))))
  );
}

function showDone(pane, reportId) {
  const done = $(".done", pane);
  done.classList.remove("hidden");
  done.replaceChildren(
    el("strong", {}, "✓ 報告を受け付けました"), " ",
    el("span", { class: "muted rid" }, reportId), " ",
    el("button", { class: "btn small", onclick: () => showView("approver", reportId) }, "承認者画面で見る")
  );
}

// ------------------------------------------------------------------ the call
class Call {
  constructor(pane, job, lang) {
    this.pane = pane;
    this.job = job;
    this.lang = lang;
    this.muted = false;
    this.sources = new Set();
    this.nextTime = 0;
    this.lastBubble = null;
    this.started = Date.now();
    this.labels = { ...(pane.fieldLabels || {}) };
  }

  async start() {
    this.keepAwake();
    // Contexts are made before any await, while the tap still counts as a user gesture
    // (iOS only unlocks audio inside one, so the playback context is resumed right away too).
    this.ctx = new AudioContext(); // playback (24 kHz buffers are resampled by the browser)
    this.ctx.resume().catch(() => {});
    // Safari 16.4+: tell iOS this page plays and records at the same time.
    try { if (navigator.audioSession) navigator.audioSession.type = "play-and-record"; } catch {}
    // Capture at 16 kHz so the browser does a proper resample; the worklet then
    // passes samples through. Fallback: the mic shares the playback context and the
    // worklet downsamples. Used on WebKit (Safari, every iOS browser), which has had
    // bugs with a mic in a context whose rate differs from the device, and when a
    // browser (Firefox) refuses such a mic source.
    let micCtx = null;
    if (!IS_WEBKIT) {
      try { micCtx = new AudioContext({ sampleRate: 16000 }); } catch { micCtx = null; }
    }
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    this.stream.getAudioTracks()[0]?.addEventListener("ended", () => {
      if (!this.cleaned) this.sysline("マイクが停止しました。「終了」して、もう一度かけ直してください（続きから再開します）。", true);
    });
    let src = null;
    if (micCtx) {
      try { src = micCtx.createMediaStreamSource(this.stream); } catch { micCtx.close(); micCtx = null; }
    }
    this.micCtx = micCtx || this.ctx;
    if (!src) src = this.micCtx.createMediaStreamSource(this.stream);
    await this.micCtx.audioWorklet.addModule("/static/pcm-worklet.js");
    await Promise.all([this.ctx.resume(), this.micCtx.resume()]);
    this.node = new AudioWorkletNode(this.micCtx, "pcm-recorder", { processorOptions: { targetRate: 16000 } });
    const sink = this.micCtx.createGain();
    sink.gain.value = 0;
    src.connect(this.node).connect(sink).connect(this.micCtx.destination);
    this.node.port.onmessage = (e) => {
      const level = this.muted ? 0 : Math.min(1, e.data.level * 6);
      $(".meter span", this.pane).style.width = `${level * 100}%`;
      if (!this.muted && this.ws?.readyState === WebSocket.OPEN && this.connected) this.ws.send(e.data.pcm);
    };

    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${proto}://${location.host}/ws/report/${this.job.job_id}?lang=${this.lang}`);
    this.ws.binaryType = "arraybuffer";
    this.ws.onmessage = (e) => {
      this.heard = true;
      typeof e.data === "string" ? this.onEvent(JSON.parse(e.data)) : this.play(e.data);
    };
    this.ws.onclose = () => {
      // The server always sends something first (status or error), so silence means we never got through.
      if (!this.heard) this.sysline(connectHelp(), true);
      this.cleanup();
    };
    this.timer = setInterval(() => this.tick(), 250);
  }

  // Keep the screen on: a phone that locks suspends the page and the call drops.
  keepAwake() {
    if (!navigator.wakeLock || document.visibilityState !== "visible") return;
    navigator.wakeLock.request("screen").then(
      (lock) => (this.cleaned ? lock.release() : (this.wakeLock = lock)),
      () => {});
  }

  // iOS suspends audio while the page is hidden or a phone call comes in; resume when we can.
  wake() {
    if (this.cleaned) return;
    for (const c of new Set([this.ctx, this.micCtx])) {
      if (c && c.state !== "running" && c.state !== "closed") c.resume().catch(() => {});
    }
    if (!this.wakeLock || this.wakeLock.released) this.keepAwake();
  }

  tick() {
    $(".timer", this.pane).textContent = fmtTime((Date.now() - this.started) / 1000);
    const speaking = this.sources.size > 0;
    const bar = $(".callbar", this.pane);
    if (this.connected) {
      bar.classList.toggle("speaking", speaking);
      bar.classList.toggle("listening", !speaking);
      $(".status", this.pane).textContent = speaking ? "話しています" : this.muted ? "ミュート中" : "聞いています";
    }
  }

  play(buffer) {
    if (buffer.byteLength % 2) buffer = buffer.slice(0, buffer.byteLength - 1);
    const pcm = new Int16Array(buffer);
    const f32 = new Float32Array(pcm.length);
    for (let i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
    const buf = this.ctx.createBuffer(1, f32.length, 24000);
    buf.copyToChannel(f32, 0);
    const s = this.ctx.createBufferSource();
    s.buffer = buf;
    s.connect(this.ctx.destination);
    const t = Math.max(this.ctx.currentTime + 0.03, this.nextTime);
    s.start(t);
    this.nextTime = t + buf.duration;
    this.sources.add(s);
    s.onended = () => this.sources.delete(s);
  }

  stopPlayback() {
    this.sources.forEach((s) => { try { s.stop(); } catch {} });
    this.sources.clear();
    this.nextTime = 0;
  }

  transcript(role, delta) {
    const box = $(".transcript", this.pane);
    appendFollow(box, () => {
      if (!this.lastBubble || this.lastBubble.dataset.role !== role) {
        this.lastBubble = el("div", { class: `bubble ${role}`, "data-role": role },
          el("span", { class: "who" }, role === "agent" ? "オペレーター" : "あなた"), el("span", { class: "t" }));
        box.append(this.lastBubble);
      }
      $(".t", this.lastBubble).textContent += delta;
    });
  }

  sysline(text, err) {
    const box = $(".transcript", this.pane);
    appendFollow(box, () => box.append(el("div", { class: `sysline ${err ? "err" : ""}` }, text)));
    this.lastBubble = null;
  }

  onEvent(ev) {
    switch (ev.type) {
      case "state":
        ev.state.fields.forEach((f) => (this.labels[f.key] = f.label));
        renderState(this.pane, ev.state, true);
        break;
      case "status":
        if (ev.status === "connected") {
          this.connected = true;
          $(".transcript", this.pane).replaceChildren();
          this.sysline(`接続しました（${ev.model}）`);
        } else if (ev.status === "time_warning") this.sysline("残り1分です");
        else if (ev.status === "time_limit") this.sysline("通話時間の上限に達しました");
        else if (ev.status === "go_away") this.sysline("サーバーから切断予告がありました");
        else if (ev.status === "language_hint_unsupported") this.sysline("⚠ 入力言語の指定がサーバーに受け付けられず、自動判定で接続しました", true);
        break;
      case "transcript":
        this.transcript(ev.role, ev.delta);
        break;
      case "interrupted":
        this.stopPlayback();
        break;
      case "turn_complete":
        this.lastBubble = null;
        break;
      case "tool":
        this.onTool(ev);
        break;
      case "usage":
        $(".cost", this.pane).textContent = `$${ev.cost_usd.toFixed(3)}`;
        break;
      case "finalized":
        this.finalizedId = ev.report_id;
        showDone(this.pane, ev.report_id);
        break;
      case "draft_saved":
        this.sysline("途中までの内容を保存しました。もう一度かけると続きから再開します。");
        break;
      case "error":
        this.sysline(`エラー: ${ev.message}`, true);
        break;
      case "ended":
        this.sysline(`通話終了（${ev.meta?.duration_sec ?? "-"}秒・推定 $${(ev.meta?.estimated_cost_usd ?? 0).toFixed(3)}）`);
        break;
    }
  }

  onTool(ev) {
    const r = ev.result || {};
    let text;
    if (ev.name === "update_report") {
      const names = (r.saved || []).map((k) => this.labels[k] || k);
      text = names.length ? `📝 記録：${names.join("、")}` : null;
      if (r.rejected?.length) text = `${text ? text + "　" : ""}⚠ ${r.rejected.map((x) => `${this.labels[x.key] || x.key}: ${x.reason}`).join(" / ")}`;
    } else if (ev.name === "set_cost_items") text = `💴 費用項目を記録（合計 ${yen(r.total)}）`;
    else if (ev.name === "record_exemption") text = r.ok ? `⏭ 免除：${ev.args?.reason}` : `⚠ 免除不可：${r.error}`;
    else if (ev.name === "finalize_report") text = r.ok ? "✅ 報告を確定しました" : `⚠ 確定できません：${r.error}`;
    if (text) this.sysline(text, r.ok === false);
  }

  sendText(text) {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.lastBubble = null;
    this.transcript("worker", text);
    this.lastBubble = null;
    this.ws.send(JSON.stringify({ type: "text", text }));
  }

  toggleMute() {
    this.muted = !this.muted;
    if (this.muted && this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "audio_end" }));
    $(".mute", this.pane).textContent = this.muted ? "🎙 ミュート解除" : "🔇 ミュート";
  }

  stop() {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "stop" }));
    else this.cleanup();
  }

  cleanup() {
    if (this.cleaned) return;
    this.cleaned = true;
    clearInterval(this.timer);
    this.wakeLock?.release().catch(() => {});
    dockObserver.disconnect();
    this.pane.classList.remove("calling");
    $(".incall", this.pane).style.transform = "";
    this.stream?.getTracks().forEach((t) => t.stop());
    // Let the closing words finish playing before closing audio.
    const wait = Math.max(0, (this.nextTime - (this.ctx?.currentTime || 0)) * 1000) + 300;
    if (this.micCtx && this.micCtx !== this.ctx) this.micCtx.close();
    setTimeout(() => this.ctx?.close(), wait);
    $(".callbar", this.pane).classList.remove("speaking", "listening");
    $(".status", this.pane).textContent = "終了";
    $(".meter span", this.pane).style.width = "0";
    $(".typebox", this.pane).classList.add("hidden");
    $(".stop", this.pane).disabled = true;
    $(".mute", this.pane).disabled = true;
    call = null;
    loadJobs().then((jobs) => {
      const job = jobs.find((j) => j.job_id === this.job.job_id);
      if (!this.finalizedId && job) {
        $(".precall", this.pane).classList.remove("hidden");
        $(".incall", this.pane).classList.add("hidden");
        selectedJob = job;
        $(".start", this.pane).onclick = () => startCall(this.pane, job, $(".lang", this.pane).value);
        api(`/api/jobs/${job.job_id}/checklist`).then((p) => $(".resume", this.pane).classList.toggle("hidden", !p.resumed));
      }
    });
  }
}

// Keep the newest line in view unless the reader has scrolled up. On a phone the page itself scrolls.
function appendFollow(box, add) {
  const page = PHONE.matches;
  const root = document.scrollingElement;
  const atEnd = page
    ? root.scrollTop + innerHeight >= root.scrollHeight - 120
    : box.scrollTop + box.clientHeight >= box.scrollHeight - 60;
  add();
  if (!atEnd || !box.offsetParent) return; // scrolled up, or the 報告書 tab is showing
  if (page) root.scrollTop = root.scrollHeight;
  else box.scrollTop = box.scrollHeight;
}

function connectHelp() {
  if (location.protocol !== "https:") return "サーバーに接続できませんでした。";
  return [
    "サーバーに接続できませんでした。iPhone では最初に証明書のインストールが必要です：",
    el("a", { href: "/ca.crt" }, "証明書をダウンロード"),
    "（設定 > 一般 > VPNとデバイス管理 でインストール → 設定 > 一般 > 情報 > 証明書信頼設定 でオン）",
  ];
}

// ------------------------------------------------------------------ phone: call dock
// During a call on a phone the controls are fixed to the bottom (CSS: .calling .incall).
// The pane is padded by the dock's height, and the dock rides above the iOS keyboard.
const dockObserver = new ResizeObserver(([e]) => {
  e.target.closest(".jobpane")?.style.setProperty("--dock-h", `${e.target.offsetHeight}px`);
});

function placeDock() {
  const dock = call && $(".incall", call.pane);
  const vv = window.visualViewport;
  if (!dock || !vv) return;
  // iOS keeps the layout viewport when the keyboard opens: lift the dock by what the keyboard covers.
  // (Android Chrome resizes the page instead — interactive-widget in index.html — so this stays 0.)
  const covered = PHONE.matches && vv.scale < 1.01 ? innerHeight - vv.height - vv.offsetTop : 0;
  dock.style.transform = covered > 1 ? `translateY(${-covered}px)` : "";
}
window.visualViewport?.addEventListener("resize", placeDock);
window.visualViewport?.addEventListener("scroll", placeDock);
document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && call?.wake());
document.addEventListener("pointerdown", () => call?.wake()); // a tap is a gesture iOS accepts for resuming audio

async function startCall(pane, job, lang) {
  if (call) return;
  if (!CONFIG.api_key_configured) { alert("GEMINI_API_KEY が .env に設定されていません"); return; }
  if (!micAvailable()) { alert("マイクは https:// のページでしか使えません。画面の案内を確認してください。"); return; }
  $(".precall", pane).classList.add("hidden");
  $(".incall", pane).classList.remove("hidden");
  $(".typebox", pane).classList.remove("hidden");
  $(".stop", pane).disabled = false;
  $(".mute", pane).disabled = false;
  pane.classList.add("calling");
  dockObserver.observe($(".incall", pane));
  setPaneTab(pane, "transcript");
  call = new Call(pane, job, lang);
  $(".stop", pane).onclick = () => call?.stop();
  $(".mute", pane).onclick = () => call?.toggleMute();
  $(".typebox", pane).onsubmit = (e) => {
    e.preventDefault();
    const input = $(".typebox input", pane);
    if (input.value.trim()) call?.sendText(input.value.trim());
    input.value = "";
  };
  try {
    await call.start();
  } catch (err) {
    call.sysline(`マイクを開始できません: ${err.message}`, true);
    call.cleanup();
  }
}

// ------------------------------------------------------------------ approver
async function loadReports() {
  const reports = await api("/api/reports");
  const box = $("#reports");
  if (!reports.length) { box.replaceChildren(el("div", { class: "muted small" }, "まだ報告はありません。")); return; }
  box.replaceChildren(
    ...reports.map((r) =>
      el("button", { class: "jobcard", "data-id": r.report_id, onclick: () => { pushDetail(); openReport(r.report_id); } },
        el("div", { class: "top" }, el("span", {}, `${r.job.large}／${r.job.small}`),
          el("span", { class: `badge ${r.completeness}` }, COMPLETENESS[r.completeness])),
        el("div", { class: "name" }, r.job.store_name),
        el("div", { class: "snippet" }, `${fmtDate(r.submitted_at)}　${yen(r.cost_total)}　注意 ${r.flags}件`)))
  );
}

async function openReport(id) {
  history.replaceState(null, "", `#approver/${id}`);
  $$("#reports .jobcard").forEach((c) => c.classList.toggle("active", c.dataset.id === id));
  const r = await api(`/api/reports/${encodeURIComponent(id)}`);
  const q = r.qnavi_fields;
  const conv = r.conversation || {};
  showDetail("approver");
  $("#reportPane").replaceChildren(
    el("button", { type: "button", class: "btn ghost small back", onclick: () => backToList("approver") }, "← 報告一覧"),
    el("div", { class: "jobhead" },
      el("div", { class: "issue" }, `${r.job.large}／${r.job.small}　#${r.job.trouble_number}`),
      el("h2", {}, r.job.store_name, " ", el("span", { class: `badge ${r.completeness}` }, COMPLETENESS[r.completeness])),
      el("div", { class: "meta" }, `${r.report_id}　受付 ${fmtDate(r.submitted_at)}　作業者 ${r.job.worker.name}さん　ルール v${r.rules_version}`),
      el("blockquote", { class: "desc" }, `店舗からの依頼：${r.job.description}`)),

    el("section", { class: "card" }, el("h3", {}, "確認ポイント ", el("small", {}, "Điểm cần chú ý (tự động từ rule)")),
      r.approver_flags.length
        ? el("ul", { class: "flags" }, r.approver_flags.map((f) => el("li", { class: f.level }, f.text)))
        : el("div", { class: "muted small" }, "特記事項なし")),

    el("section", { class: "card" }, el("h3", {}, "QNavi 報告欄 ", el("small", {}, "Map thẳng vào trường của hệ thống")),
      el("div", { class: "qnavi" }, ["report_symptom", "report_reason", "report_content"].map((k) =>
        el("div", { class: "col" }, el("div", { class: "k" }, k), el("div", { class: "v" }, q[k] || "—"))))),

    el("section", { class: "card" }, el("h3", {}, "報告項目"),
      el("div", { class: "tablewrap" }, el("table", { class: "stack" },
        el("thead", {}, el("tr", {}, el("th", {}, "項目"), el("th", {}, "内容"), el("th", {}, "区分"))),
        el("tbody", {}, r.fields.map((f) => el("tr", {}, el("td", {}, f.label), el("td", {}, f.display), el("td", { class: "muted small" }, f.source))))))),

    el("section", { class: "card" }, el("h3", {}, "費用項目"),
      r.cost_items.length
        ? el("div", { class: "tablewrap" }, el("table", {},
            el("thead", {}, el("tr", {}, el("th", {}, "項目"), el("th", {}, "区分"), el("th", { class: "num" }, "数量"), el("th", { class: "num" }, "金額"), el("th", {}, "メモ"))),
            el("tbody", {}, r.cost_items.map((i) => el("tr", {}, el("td", {}, i.label), el("td", {}, i.cost_group_ja), el("td", { class: "num" }, i.quantity), el("td", { class: "num" }, yen(i.amount)), el("td", { class: "small" }, i.note)))),
            el("tfoot", {}, el("tr", {}, el("th", { colspan: 3 }, "合計（税抜）"), el("th", { class: "num" }, yen(r.cost_total)), el("th")))))
        : el("div", { class: "muted small" }, "費用項目なし（免除の理由は確認ポイント参照）")),

    el("section", { class: "card" },
      el("details", {}, el("summary", {}, `会話ログ（${r.transcript.length}件）`),
        el("div", { class: "transcript" }, r.transcript.map((t) =>
          el("div", { class: `bubble ${t.role}` }, el("span", { class: "who" }, `${t.role === "agent" ? "オペレーター" : "作業者"}${t.typed ? "（入力）" : ""}`), t.text)))),
      el("details", {}, el("summary", {}, "approve-helper 連携用データ"),
        el("p", { class: "muted small" }, "POST /api/v2/estimation-evaluation にそのまま渡せる形式です（デモでは送信しません）。"),
        el("pre", { class: "code" }, JSON.stringify(r.approve_helper_payload, null, 2))),
      el("details", {}, el("summary", {}, "通話情報"),
        el("dl", { class: "kv" },
          el("dt", {}, "モデル"), el("dd", {}, conv.model),
          el("dt", {}, "言語"), el("dd", {}, conv.language),
          el("dt", {}, "通話時間"), el("dd", {}, `${conv.duration_sec}秒`),
          el("dt", {}, "トークン"), el("dd", {}, JSON.stringify(conv.usage_tokens)),
          el("dt", {}, "推定コスト"), el("dd", {}, `$${(conv.estimated_cost_usd || 0).toFixed(4)}`),
          el("dt", {}, "続きから再開"), el("dd", {}, conv.resumed_from_draft ? "はい" : "いいえ"))),
      el("details", {}, el("summary", {}, "JSON（保存データ）"), el("pre", { class: "code" }, JSON.stringify(r, null, 2))))
  );
}

// ------------------------------------------------------------------ rules
async function loadRules() {
  const r = await api("/api/rules");
  $("#rulesMeta").textContent = `v${r.version}`;
  $("#rulesPath").textContent = r.path;
  $("#rulesYaml").textContent = r.yaml;
  const jobs = await api("/api/jobs");
  $("#promptJob").replaceChildren(...jobs.map((j) => el("option", { value: j.job_id }, `${j.job_id} ${j.large}／${j.small} ${j.store_name}`)));
}
$("#reloadRules").onclick = async () => {
  const msg = $("#reloadMsg");
  try {
    const r = await api("/api/rules/reload", { method: "POST" });
    msg.replaceChildren(el("div", { class: "msg-ok" }, `✓ 再読み込みしました（v${r.version}）。次の通話から適用されます。`));
    loadRules();
  } catch (err) {
    msg.replaceChildren(el("div", { class: "msg-err" }, `✗ ${err.message}`));
  }
};
$("#showPrompt").onclick = async () => {
  $("#promptText").textContent = await api(`/api/jobs/${$("#promptJob").value}/prompt?lang=${$("#promptLang").value}`);
};

// ------------------------------------------------------------------ boot
(async () => {
  CONFIG = await api("/api/config");
  $("#modelBadge").replaceChildren(el("span", {},
    el("span", { class: "mname" }, `model: ${CONFIG.model}`),
    !CONFIG.api_key_configured && el("span", { class: "warn" }, "　APIキー未設定")));
  showView(...parseHash());
})();
