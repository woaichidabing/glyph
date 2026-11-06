/* Markr 测试 web 台 · 前端逻辑（方向 C · 阅卷纸感）
 *
 * 通过 fetch + ReadableStream 消费 /api/ask 的 SSE 流，按事件类型渲染（events.py 权威）：
 *   queued / run_started / text_delta / tool_call / tool_result / web_search /
 *   turn_retry / result / error（assistant_message 现阶段忽略，G7）
 *
 * 两条业务线（MSG-010）：
 *   图片 → run1 OCR 转写 → 转写确认台（人闸）→ run2 grade_text → 裁决台
 *          → 驳回 → run3 recheck_detection（走聊天通道，按 id 原地回卡，绝不新建卡片）
 *   txt/md → 文本确认台（同一骨架）→ run2 批改
 *
 * 契约：docs/design/teacher-grading-addendum.md §6 · docs/design/ui/web-ui-spec.md
 * 隐私口径（MSG-009）：文案不出现真实姓名 / 分数。
 */
(() => {
  "use strict";

  // ---------- DOM ----------
  const $ = (id) => document.getElementById(id);
  const chatBody = $("chat-body");
  const chatInner = $("chat-inner");
  const welcome = $("welcome");
  const chatTitle = $("chat-title");
  const input = $("input");
  const sendBtn = $("btn-send");
  const sendIcon = $("send-icon");
  const stopIcon = $("stop-icon");
  const attachmentsEl = $("attachments");
  const fileInput = $("file-input");
  const toastEl = $("toast");
  const sidebar = $("sidebar");
  const sidebarBackdrop = $("sidebar-backdrop");
  const sbList = $("sb-list");
  const batchBar = $("batch-bar");
  const modelTagText = $("model-tag-text");
  const modelPop = $("model-pop");

  // OCR 台
  const ovlOcr = $("ovl-ocr");
  const ocrTitle = $("ocr-title");
  const ocrMeta = $("ocr-meta");
  const ocrPhoto = $("ocr-photo");
  const ocrEditor = $("ocr-editor");
  const ocrWarnings = $("ocr-warnings");
  const flowStep1 = $("flow-step1");
  const flowStep2 = $("flow-step2");
  const flowStep3 = $("flow-step3");

  // 裁决台
  const ovlReview = $("ovl-review");
  const rvEssayText = $("rv-essay-text");
  const rvWordcount = $("rv-wordcount");
  const rvSumMeta = $("rv-sum-meta");
  const rvSummaryText = $("rv-summary-text");
  const rvStats = $("rv-stats");
  const rvWarnings = $("rv-warnings");
  // 批阅稿浮层（MSG-015）
  const ovlMarksheet = $("ovl-marksheet");
  const marksheetBody = $("marksheet-body");
  const marksheetWarn = $("marksheet-warn");
  let _marksheetResp = null; // 缓存 finalize 响应，批阅稿底部按钮复用
  const rvQuota = $("rv-quota");
  const rvProgFill = $("rv-prog-fill");
  const rvProgText = $("rv-prog-text");
  const rvCards = $("rv-cards");
  const rvComment = $("rv-comment");
  const rvCopyNote = $("rv-copy-note");
  const rvPreviewPre = $("rv-preview-pre");

  // ---------- 状态 ----------
  const state = {
    running: false,
    abortController: null,
    models: {}, // /api/tools 下发：{chat, grade, vision}
    sessions: [],
    batchMode: false,
    selectedIds: new Set(),
    attachment: null, // 单附件 {resp, dataUrl}；resp = /api/upload 响应
    pendingImage: null, // run1 发出后待开台：{filename, size, mime, dataUrl}
    turn: null, // 当前轮草稿（turn_retry 回退用）
    ocr: null, // 确认台数据 {mode:'ocr'|'text', ...}
    review: null, // 裁决台数据
    focusIndex: -1,
  };

  // grade_text 工具卡 note 里的进度文字节点（MSG-016：随裁决进度实时变三态）
  let _reviewNoteProgressEl = null;

  // ---------- 小工具 ----------
  function escapeHtml(s) {
    return String(s ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function el(tag, cls, html) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html != null) node.innerHTML = html;
    return node;
  }

  function fmtSize(bytes) {
    if (bytes == null) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  let toastTimer = null;
  function toast(msg, ok) {
    toastEl.className = "toast" + (ok ? " ok" : "");
    toastEl.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
      escapeHtml(msg);
    toastEl.style.display = "flex";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toastEl.style.display = "none"), 3200);
  }

  function scrollToBottom() {
    chatBody.scrollTop = chatBody.scrollHeight;
  }

  // 徽标文案映射（type / source → badge）
  const TYPE_LABELS = {
    grammar: ["语法", "b-hard"],
    spelling: ["拼写", "b-hard"],
    chinglish: ["中式英语", "b-chinglish"],
    wordiness: ["地道性", "b-wordy"],
    word_choice: ["选词", "b-wordy"],
    punctuation: ["标点", "b-hard"],
    other: ["其他", "b-hard"],
  };
  const SOURCE_LABELS = { model: ["模型批改", "b-model"], spell: ["拼写库", "b-spell"] };

  // ---------- 会话管理 ----------
  async function api(path, options) {
    const resp = await fetch(path, options);
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${resp.status}`);
    }
    return resp.json();
  }

  async function loadSessions() {
    try {
      const data = await api("/api/sessions");
      state.sessions = data.sessions || [];
      renderSessions();
      const current = state.sessions.find((s) => s.is_current);
      if (current) {
        chatTitle.textContent = current.title || "新的会话";
        await loadHistory(current.session_id);
      }
    } catch (e) {
      toast(`加载会话失败：${e.message}`);
    }
  }

  function renderSessions() {
    sbList.innerHTML = "";
    for (const s of state.sessions) {
      const item = el("button", "sb-item" + (s.is_current ? " current" : "") + (state.batchMode ? " selectable" : ""));
      const time = s.created_at ? new Date(s.created_at) : null;
      const timeText = time
        ? `${String(time.getMonth() + 1).padStart(2, "0")}-${String(time.getDate()).padStart(2, "0")} ${String(time.getHours()).padStart(2, "0")}:${String(time.getMinutes()).padStart(2, "0")}`
        : "";
      item.innerHTML = `
        <span class="t">${escapeHtml(s.title || "新的会话")}</span>
        <span class="m"><span>${escapeHtml(timeText)}</span>
          <span class="sb-del" title="删除会话"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg></span>
        </span>
        <input type="checkbox" class="sb-check" ${state.selectedIds.has(s.session_id) ? "checked" : ""} />`;
      item.querySelector(".sb-del").addEventListener("click", (e) => {
        e.stopPropagation();
        deleteSession(s.session_id);
      });
      const cb = item.querySelector(".sb-check");
      if (cb) {
        cb.addEventListener("click", (e) => e.stopPropagation());
        cb.addEventListener("change", () => {
          if (cb.checked) state.selectedIds.add(s.session_id);
          else state.selectedIds.delete(s.session_id);
          $("btn-batch-del").disabled = state.selectedIds.size === 0;
        });
      }
      item.addEventListener("click", () => {
        if (state.batchMode) {
          cb.checked = !cb.checked;
          cb.dispatchEvent(new Event("change"));
        } else {
          openSession(s.session_id);
        }
      });
      sbList.appendChild(item);
    }
  }

  async function loadHistory(sessionId) {
    try {
      const data = await api(`/api/sessions/${sessionId}/messages`);
      chatInner.innerHTML = "";
      chatInner.appendChild(welcome);
      welcome.style.display = (data.messages || []).length ? "none" : "flex";
      for (const m of data.messages || []) {
        if (m.role === "user") appendUserMessage(m.text);
        else appendAiText(m.text);
      }
      scrollToBottom();
    } catch (e) {
      toast(`读取会话失败：${e.message}`);
    }
  }

  async function createSession() {
    if (state.running) return toast("当前有任务正在执行，请稍候");
    try {
      const data = await api("/api/sessions/new", { method: "POST" });
      await loadSessions();
      toast("已新建会话", true);
    } catch (e) {
      toast(e.message);
    }
  }

  async function openSession(sessionId) {
    if (state.running) return toast("当前有任务正在执行，请稍候");
    if (state.sessionId === sessionId) return;
    try {
      const data = await api("/api/sessions/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
      });
      state.sessionId = data.session_id;
      await loadSessions();
    } catch (e) {
      toast(e.message);
    }
  }

  async function deleteSession(sessionId) {
    if (state.running) return toast("当前有任务正在执行，请稍候");
    try {
      await api(`/api/sessions/${sessionId}`, { method: "DELETE" });
      state.selectedIds.delete(sessionId);
      await loadSessions();
      toast("会话已删除", true);
    } catch (e) {
      toast(e.message);
    }
  }

  async function batchDelete() {
    if (!state.selectedIds.size) return;
    try {
      const data = await api("/api/sessions/batch-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_ids: [...state.selectedIds] }),
      });
      state.selectedIds.clear();
      $("btn-batch-del").disabled = true;
      $("select-all-cb").checked = false;
      await loadSessions();
      toast(`已删除 ${data.deleted.length} 个会话`, true);
    } catch (e) {
      toast(e.message);
    }
  }

  function toggleBatchMode() {
    state.batchMode = !state.batchMode;
    state.selectedIds.clear();
    $("btn-batch-del").disabled = true;
    $("btn-batch").classList.toggle("on", state.batchMode);
    batchBar.classList.toggle("show", state.batchMode);
    renderSessions();
  }

  function openSidebar() {
    sidebar.classList.add("open");
    sidebarBackdrop.classList.add("show");
  }
  function closeSidebar() {
    sidebar.classList.remove("open");
    sidebarBackdrop.classList.remove("show");
  }

  // ---------- 消息渲染 ----------
  const LOGO_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19V5l8 7 8-7v14"/></svg>';
  const IMG_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg>';
  const DOC_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/></svg>';

  function appendUserMessage(text, attachment) {
    welcome.style.display = "none";
    const msg = el("div", "msg-user");
    let html = "";
    if (attachment) {
      const sub = attachment.resp.channel === "ocr"
        ? `${fmtSize(attachment.resp.size)} · 将走 OCR 转写`
        : `${fmtSize(attachment.resp.size)} · ${attachment.resp.filename}`;
      const thumb = attachment.dataUrl
        ? `<img src="${attachment.dataUrl}" alt="" />`
        : IMG_ICON;
      html += `<div class="u-att"><span class="att-chip"><span class="att-thumb">${thumb}</span><span class="att-meta"><span class="att-name">${escapeHtml(attachment.resp.filename)}</span><span class="att-sub">${escapeHtml(sub)}</span></span></span></div>`;
    }
    html += `<div class="u-text">${escapeHtml(text)}</div>`;
    msg.innerHTML = html;
    chatInner.appendChild(msg);
    scrollToBottom();
  }

  function appendAiText(text) {
    const msg = el("div", "msg-ai");
    msg.innerHTML = `<div class="ai-row"><div class="ai-dot">${LOGO_SVG}</div><div class="ai-col"><div class="block-text">${escapeHtml(text)}</div></div></div>`;
    chatInner.appendChild(msg);
  }

  function ensureAiMsg() {
    if (state.turn && state.turn.aiMsg) return state.turn;
    const msg = el("div", "msg-ai");
    msg.innerHTML = `<div class="ai-row"><div class="ai-dot">${LOGO_SVG}</div><div class="ai-col"></div></div>`;
    chatInner.appendChild(msg);
    scrollToBottom();
    if (!state.turn) state.turn = {};
    state.turn.aiMsg = msg;
    state.turn.col = msg.querySelector(".ai-col");
    state.turn.textEl = null;
    state.turn.cards = new Map();
    state.turn.searches = [];
    state.turn.startTime = Date.now();
    state.turn.searchCount = 0;
    return state.turn;
  }

  function appendTextDelta(delta) {
    const turn = ensureAiMsg();
    if (!turn.textEl) {
      turn.textEl = el("div", "block-text typing");
      turn.col.appendChild(turn.textEl);
    }
    turn.textEl.textContent += delta;
    scrollToBottom();
  }

  function removeTypingCursor() {
    if (state.turn && state.turn.textEl) state.turn.textEl.classList.remove("typing");
  }

  function addGateNote() {
    chatInner.appendChild(
      el(
        "div",
        "gate-note",
        '<span class="gn-line"></span><span class="gn-ok">✓</span><span>转写已确认 —— 确认后的文本才进入批改（流程闸）</span><span class="gn-line"></span>'
      )
    );
    scrollToBottom();
  }

  // ---------- 工具卡 ----------
  const GEAR_SVG =
    '<svg class="tc-gear" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';

  function addToolCard(call) {
    const turn = ensureAiMsg();
    removeTypingCursor();
    turn.textEl = null;
    const card = el("div", "tool-card open");
    card.innerHTML = `
      <div class="tc-head">${GEAR_SVG}<span class="tc-name">${escapeHtml(call.name)}</span><span class="tc-pill run">运行中</span><svg class="tc-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></div>
      <div class="tc-body"><div class="tc-params"></div></div>`;
    card.querySelector(".tc-head").addEventListener("click", () => card.classList.toggle("open"));
    turn.col.appendChild(card);
    turn.cards.set(call.id, { el: card, name: call.name, args: call.arguments || {} });
    scrollToBottom();
    return card;
  }

  function updateToolCard(ev) {
    const turn = state.turn;
    if (!turn) return;
    const entry = turn.cards.get(ev.call_id);
    if (!entry) return;
    const card = entry.el;
    const pill = card.querySelector(".tc-pill");
    pill.className = "tc-pill " + (ev.ok ? "done" : "fail");
    pill.textContent = ev.ok ? "完成" : "失败";
    card.querySelector(".tc-dur")?.remove();
    // 耗时插在状态 pill 之后（head 里已有一个 chevron，不重复插）
    pill.insertAdjacentHTML("afterend", `<span class="tc-dur">${(ev.duration_ms / 1000).toFixed(1)}s</span>`);
    const body = card.querySelector(".tc-body");
    body.querySelector(".tc-note")?.remove();

    const note = document.createElement("div");
    if (!ev.ok) {
      note.className = "tc-note err";
      note.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>' +
        `<span>${escapeHtml(ev.error || "工具执行失败")} · 已把错误回灌给模型换招。</span>`;
      body.appendChild(note);
      if (ev.name === "recheck_detection") recheckFailed(ev);
      return;
    }

    note.className = "tc-note";
    const out = ev.output || {};
    if (ev.name === "ocr_image") {
      const n = (out.low_confidence || []).length;
      const lowText = n ? ` · ${n} 处低置信已标 <b>[?]</b>` : "";
      note.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>' +
        `<span>转写完成${lowText} · <span class="tc-open-wb" data-wb="ocr">已打开「转写确认工作台」</span>，等你核对后才开始批改。</span>`;
      openOcrWorkbench(out, ev.duration_ms);
    } else if (ev.name === "grade_text") {
      const stats = out.stats || {};
      const total = stats.total ?? (out.detections || []).length;
      note.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>' +
        `<span>共 ${total} 处建议（要改 ${stats.error_count ?? 0} · 建议 ${stats.suggestion_count ?? 0}）· <span class="tc-review-progress tc-open-wb" data-wb="review">已打开「批改裁决工作台」，逐条确认后生成定稿 →</span></span>`;
      // 把进度文字节点挂到模块级，openReviewWorkbench 会转给 state.review
      _reviewNoteProgressEl = note.querySelector(".tc-review-progress");
      openReviewWorkbench(out, entry.args.text || "");
    } else if (ev.name === "recheck_detection") {
      note.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>' +
        `<span>复核完成（第 ${out.round ?? "?"} 轮）· 结论已回填到裁决台的对应卡片。</span>`;
      applyRecheckResult(out);
    } else {
      note.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>' +
        `<span>执行成功。</span>`;
      if (out && (out.result !== undefined || out.words !== undefined)) {
        body.querySelector(".tc-params").textContent = JSON.stringify(out, null, 2);
      }
    }
    body.appendChild(note);
    card.classList.add("open");
    scrollToBottom();
  }

  // ---------- 联网搜索卡（web_search：只展示，不入账本） ----------
  function updateSearchCard(ev) {
    const turn = ensureAiMsg();
    let card = turn.searches[turn.searches.length - 1];
    if (ev.phase === "in_progress") {
      card = el("div", "search-card");
      card.innerHTML = `<div class="sc-line"><svg class="sc-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.35-4.35"/></svg><span style="color:var(--fg2)">联网搜索已开启</span><span class="sc-phase">in_progress</span></div>`;
      turn.col.appendChild(card);
      turn.searches.push(card);
      scrollToBottom();
      return;
    }
    if (!card) return;
    if (ev.phase === "searching") {
      card.innerHTML = `<div class="sc-line"><span class="sc-spin"></span><span class="sc-query">搜索中：<b>${escapeHtml(ev.query || "")}</b></span><span class="sc-phase">searching</span></div>`;
      scrollToBottom();
      return;
    }
    if (ev.phase === "completed") {
      turn.searchCount += 1;
      const sources = ev.sources || [];
      const host = (u) => {
        try {
          return new URL(u).hostname.replace(/^www\./, "");
        } catch {
          return u;
        }
      };
      const rows = sources
        .map(
          (s) =>
            `<div class="sc-src"><a href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</a><span class="su">${escapeHtml(host(s.url))}</span></div>`
        )
        .join("");
      const more =
        sources.length > 5
          ? `<button class="sc-more">展开全部 ${sources.length} 条</button>`
          : "";
      card.innerHTML =
        `<div class="sc-line"><svg class="sc-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.35-4.35"/></svg><span class="sc-query">搜索完成 · <b>${sources.length} 个来源</b></span><span class="sc-phase">completed</span></div>` +
        (rows ? `<div class="sc-sources">${rows}${more}</div>` : "");
      if (more) {
        // 默认只展示前 5 条来源，点按钮展开/收起尾部
        const all = card.querySelectorAll(".sc-src");
        all.forEach((r, i) => {
          if (i >= 5) r.style.display = "none";
        });
        const btn = card.querySelector(".sc-more");
        btn.addEventListener("click", () => {
          const expanded = all[5].style.display !== "none";
          all.forEach((r, i) => {
            if (i >= 5) r.style.display = expanded ? "none" : "flex";
          });
          btn.textContent = expanded ? `展开全部 ${sources.length} 条` : "收起";
        });
      }
      scrollToBottom();
    }
  }

  // ---------- 终态 / 错误 / 重试 ----------
  function appendResultMeta(ev) {
    const turn = state.turn;
    removeTypingCursor();
    const turnEl = turn && turn.aiMsg ? turn : ensureAiMsg();
    const meta = el("div", "result-meta");
    const secs = turnEl.startTime ? ((Date.now() - turnEl.startTime) / 1000).toFixed(1) + "s" : "";
    const usage = ev.usage;
    const tok = usage ? `↑${usage.input_tokens ?? 0} ↓${usage.output_tokens ?? 0} tok` : "";
    let html = "";
    if (ev.finish_reason === "final_text") html += '<span class="rm-chip ok">final_text</span>';
    else html += `<span class="rm-chip">${escapeHtml(ev.finish_reason)}</span>`;
    if (secs) html += `<span class="rm-chip">${secs}</span>`;
    if (ev.iterations > 1) html += `<span class="rm-chip">${ev.iterations} 轮</span>`;
    if (tok) html += `<span class="rm-chip">${tok}</span>`;
    if (ev.web_search_count > 0)
      html += `<span class="rm-chip"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.35-4.35"/></svg>${ev.web_search_count} 次</span>`;
    html +=
      '<button class="rm-copy"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>复制</button>';
    meta.innerHTML = html;
    meta.querySelector(".rm-copy").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(ev.text || "");
        toast("已复制", true);
      } catch {
        toast("复制失败：浏览器未授权剪贴板");
      }
    });
    turnEl.col.appendChild(meta);
    scrollToBottom();
  }

  function appendError(message) {
    chatInner.appendChild(
      el(
        "div",
        "error-bar",
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/></svg>' +
          `<span>${escapeHtml(message)}</span>`
      )
    );
    scrollToBottom();
  }

  const RETRY_TEXT = {
    network: "网络中断，正在重发本轮…",
    idle_timeout: "模型长时间无响应，正在重发本轮…",
    max_tokens: "输出超长，已自动升档重发本轮…",
  };

  function showRetryNotice(ev) {
    chatInner.appendChild(
      el(
        "div",
        "retry-notice",
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>' +
          `<span>${escapeHtml(RETRY_TEXT[ev.reason] || "正在重发本轮…")}（第 ${ev.attempt} 次）</span>`
      )
    );
    scrollToBottom();
  }

  // turn_retry：本轮未确认内容全部作废重来（MSG-003/004）
  function rollbackTurnDraft() {
    const turn = state.turn;
    if (!turn) return;
    if (turn.textEl) {
      turn.textEl.remove();
      turn.textEl = null;
    }
    if (turn.cards) {
      for (const { el: card } of turn.cards.values()) card.remove();
      turn.cards = new Map();
    }
    if (turn.searches) {
      for (const s of turn.searches) s.remove();
      turn.searches = [];
    }
    turn.searchCount = 0;
    scrollToBottom();
  }

  // ---------- SSE 主流程 ----------
  async function consumeSSE(reader) {
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          try {
            handleEvent(JSON.parse(line.slice(6)));
          } catch {
            // 忽略解析失败的行
          }
        }
      }
    }
    if (state.running) setRunning(false);
  }

  function handleEvent(ev) {
    switch (ev.type) {
      case "queued":
        toast(ev.message || "已排队等待…");
        break;
      case "run_started":
        ensureAiMsg();
        break;
      case "text_delta":
        appendTextDelta(ev.delta);
        break;
      case "tool_call":
        addToolCard(ev.call);
        break;
      case "tool_result":
        updateToolCard(ev);
        break;
      case "web_search":
        updateSearchCard(ev);
        break;
      case "turn_retry":
        rollbackTurnDraft();
        showRetryNotice(ev);
        break;
      case "result":
        appendResultMeta(ev);
        setRunning(false);
        settleChecks();
        break;
      case "error":
        appendError(ev.message);
        setRunning(false);
        settleChecks();
        break;
      default:
        // assistant_message 等状态事件现阶段无需 UI 动作（G7）
        break;
    }
  }

  // run 结束（result/error/断流）：把所有「复核中」卡复位回待裁决
  function settleChecks() {
    const rv = state.review;
    if (!rv || !rv.checkingIds || rv.checkingIds.size === 0) return;
    for (const id of rv.checkingIds) {
      renderCard(id);
    }
    rv.checkingIds.clear();
  }

  function setRunning(on) {
    state.running = on;
    sendIcon.style.display = on ? "none" : "block";
    stopIcon.style.display = on ? "block" : "none";
    sendBtn.title = on ? "停止" : "发送 (Enter)";
  }

  async function runChat(message) {
    if (state.running) {
      toast("当前有任务正在执行，请稍候");
      return false;
    }
    appendUserMessage(message);
    state.turn = null;
    setRunning(true);
    state.abortController = new AbortController();
    try {
      // 统一走 /api/ask（multipart）：纯文本就只带 text；图片此前已由 /api/upload 换成
      // image_id 并拼进 message，服务端按同一条消息处理。不手动设 Content-Type，交给浏览器
      // 写 multipart boundary。
      const fd = new FormData();
      fd.append("text", message);
      const resp = await fetch("/api/ask", {
        method: "POST",
        body: fd,
        signal: state.abortController.signal,
      });
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
      await consumeSSE(resp.body.getReader());
    } catch (err) {
      if (err.name !== "AbortError") {
        appendError(`连接失败：${err.message}`);
      }
    } finally {
      setRunning(false);
      removeTypingCursor();
      settleChecks();
      state.abortController = null;
    }
    return true;
  }

  // ---------- 上传（§6.1 分流） ----------
  function renderAttachmentChip() {
    attachmentsEl.innerHTML = "";
    const att = state.attachment;
    if (!att) return;
    const resp = att.resp;
    const chip = el("span", "att-chip");
    const thumb = att.dataUrl ? `<img src="${att.dataUrl}" alt="" />` : resp.channel === "ocr" ? IMG_ICON : DOC_ICON;
    const sub =
      resp.channel === "ocr"
        ? `${fmtSize(resp.size)} · 将走 OCR 转写`
        : `${fmtSize(resp.size)} · 发送前先确认内容`;
    chip.innerHTML = `<span class="att-thumb">${thumb}</span><span class="att-meta"><span class="att-name">${escapeHtml(resp.filename)}</span><span class="att-sub">${escapeHtml(sub)}</span></span><button class="att-x" title="移除"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg></button>`;
    chip.querySelector(".att-x").addEventListener("click", () => {
      state.attachment = null;
      renderAttachmentChip();
    });
    attachmentsEl.appendChild(chip);
  }

  async function uploadFile(file) {
    const formData = new FormData();
    formData.append("file", file);
    let respData;
    try {
      respData = await api("/api/upload", { method: "POST", body: formData });
    } catch (e) {
      toast(`上传失败：${e.message}`);
      return;
    }
    if (!respData.accepted) {
      toast(respData.reason || "不支持该文件类型");
      return;
    }
    const att = { resp: respData, dataUrl: null };
    if (respData.channel === "ocr") {
      // 本地预览用 dataUrl（发送给模型的图片走服务端内存仓的 image_id）
      att.dataUrl = await new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => resolve(null);
        reader.readAsDataURL(file);
      });
    }
    state.attachment = att;
    renderAttachmentChip();
  }

  // ---------- 发送 / 停止 ----------
  function onSendOrStop() {
    if (state.running) {
      state.abortController?.abort();
      fetch("/api/cancel", { method: "POST" }).catch(() => {});
      setRunning(false);
      return;
    }
    const text = input.value.trim();
    const att = state.attachment;
    if (!text && !att) return;

    if (att && att.resp.channel === "ocr") {
      state.pendingImage = {
        filename: att.resp.filename,
        size: att.resp.size,
        mime: att.resp.content_type,
        dataUrl: att.dataUrl,
      };
      // 不再替用户下命令：这里原本写死「请转写这张图片里的英文内容」，
      // 等于前端替用户决定了要做 OCR —— 于是每一张图都强制进 OCR 工作台，
      // 而用户可能只是发张图、压根没想批改（人 2026-09-25 判定：这是错的）。
      // 现在改成陈述事实：告诉模型「这里有一张图、编号是多少」。
      // 要不要转写 / 批改，交给模型结合用户的话去判断，或让用户自己说。
      // 工具描述里的「什么时候别调 ocr_image」才是那道边界，不在这里写死。
      const notice = `（用户上传了一张图片：${att.resp.filename || "未命名"}，image_id: ${att.resp.image_id}）`;
      const full = text ? `${text}\n\n${notice}` : notice;
      state.attachment = null;
      renderAttachmentChip();
      input.value = "";
      input.style.height = "auto";
      runChat(full);
      return;
    }

    if (att && att.resp.channel === "read_file") {
      // txt/md：先过确认闸（同一确认台骨架），确认后才发批改
      openTextConfirm(att, text);
      state.attachment = null;
      renderAttachmentChip();
      input.value = "";
      input.style.height = "auto";
      return;
    }

    input.value = "";
    input.style.height = "auto";
    runChat(text);
  }

  // ---------- OCR 转写确认工作台 ----------
  function setOcrFlow(step) {
    // step: 1 上传完成（当前=确认） / 2 确认完成（当前=裁决）
    flowStep1.textContent = "上传 ✓";
    flowStep1.className = "done";
    if (step === 1) {
      flowStep2.textContent = "转写确认 ●";
      flowStep2.className = "cur";
      flowStep3.textContent = "批改裁决 ○";
      flowStep3.className = "todo";
    } else {
      flowStep2.textContent = "转写确认 ✓";
      flowStep2.className = "done";
      flowStep3.textContent = "批改裁决 ●";
      flowStep3.className = "cur";
    }
  }

  function chipHtml(text, ok) {
    return `<span class="rm-chip${ok ? " ok" : ""}">${escapeHtml(text)}</span>`;
  }

  function renderOcrEditor(text) {
    // [?] 占位 → 警示黄高亮 span（模型自曝低置信，工具不猜）
    const parts = String(text ?? "").split("[?]");
    let html = "";
    for (let i = 0; i < parts.length; i++) {
      html += escapeHtml(parts[i]);
      if (i < parts.length - 1)
        html += '<span class="lc" title="此处模型没有把握，请对照原图核对">[?]</span>';
    }
    ocrEditor.innerHTML = html;
  }

  function openOcrWorkbench(output, durationMs) {
    const pending = state.pendingImage || {};
    state.ocr = {
      mode: "ocr",
      imageId: output.image_id,
      filename: pending.filename || "图片",
      size: pending.size,
      mime: pending.mime,
      dataUrl: pending.dataUrl,
      durationMs,
    };
    ocrTitle.textContent = "OCR 转写确认";
    setOcrFlow(1);
    ocrMeta.innerHTML =
      chipHtml(state.ocr.filename) +
      chipHtml(fmtSize(state.ocr.size)) +
      chipHtml(state.ocr.mime || "") +
      chipHtml(`视觉模型 ${state.models.vision || "—"}`) +
      chipHtml(`转写 ${((durationMs || 0) / 1000).toFixed(1)}s`, true);
    ocrPhoto.innerHTML = state.ocr.dataUrl
      ? `<img src="${state.ocr.dataUrl}" alt="原图" />`
      : `<div class="file-card">${IMG_ICON}<span class="cap">原图预览不可用</span></div>`;
    renderOcrEditor(output.text);
    const n = (output.low_confidence || []).length;
    let warns = "";
    if (n)
      warns += `<div class="ocr-warn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg><span>有 ${n} 处模型没把握，已用 [?] 标出，请对照原图重点核对。</span></div>`;
    for (const w of output.warnings || [])
      warns += `<div class="ocr-warn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg><span>${escapeHtml(w)}</span></div>`;
    warns +=
      '<div class="ocr-warn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg><span>转写只做抄写，不改动、不润色。编辑后的文本才是唯一批改输入。</span></div>';
    ocrWarnings.innerHTML = warns;
    ovlOcr.classList.add("open");
  }

  function openTextConfirm(att, note) {
    state.ocr = {
      mode: "text",
      filename: att.resp.filename,
      size: att.resp.size,
      content: att.resp.content || "",
      note: note || "",
    };
    ocrTitle.textContent = "文本确认";
    flowStep1.textContent = "上传文件 ✓";
    flowStep1.className = "done";
    flowStep2.textContent = "文本确认 ●";
    flowStep2.className = "cur";
    flowStep3.textContent = "批改裁决 ○";
    flowStep3.className = "todo";
    ocrMeta.innerHTML =
      chipHtml(state.ocr.filename) + chipHtml(fmtSize(state.ocr.size)) + chipHtml("已读取内容", true);
    ocrPhoto.innerHTML = `<div class="file-card">${DOC_ICON}<span class="fc-name">${escapeHtml(state.ocr.filename)}</span><span class="fc-meta">${fmtSize(state.ocr.size)}</span></div>`;
    renderOcrEditor(state.ocr.content);
    ocrWarnings.innerHTML =
      '<div class="ocr-warn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg><span>以下是文件内容，请核对（可编辑）。确认后才会开始批改。</span></div>';
    ovlOcr.classList.add("open");
  }

  function closeOcrWorkbench() {
    ovlOcr.classList.remove("open");
  }

  async function confirmOcr() {
    const ocr = state.ocr;
    if (!ocr) return;
    const text = ocrEditor.innerText.replace(/\u00a0/g, " ").trim();
    if (!text) return toast("内容为空，请先核对或编辑转写文本");
    const remaining = (text.match(/\[\?\]/g) || []).length;
    if (remaining) toast(`还有 ${remaining} 处 [?] 未核对，将按当前文本继续批改`);
    closeOcrWorkbench();
    addGateNote();
    const prefix = ocr.note ? ocr.note + "\n\n" : "";
    state.ocr = null;
    state.pendingImage = null;
    await runChat(`${prefix}请批改：\n${text}`);
  }

  function retryOcr() {
    const ocr = state.ocr;
    if (!ocr || ocr.mode !== "ocr") return;
    if (state.running) return toast("当前有任务正在执行，请稍候");
    ocrEditor.setAttribute("contenteditable", "false");
    ocrEditor.style.opacity = ".55";
    runChat(`请转写这张图片里的英文内容（image_id: ${ocr.imageId}）`).then(() => {
      ocrEditor.setAttribute("contenteditable", "true");
      ocrEditor.style.opacity = "";
    });
    // 新的 ocr tool_result 到达时 openOcrWorkbench 会重填编辑器
    state.pendingImage = {
      filename: ocr.filename,
      size: ocr.size,
      mime: ocr.mime,
      dataUrl: ocr.dataUrl,
    };
  }

  // ---------- 批改裁决工作台 ----------
  function openReviewWorkbench(output, essayText) {
    state.review = {
      essay: essayText,
      detections: output.detections || [],
      decisions: {},
      overrides: {},
      summary: output.summary || "",
      stats: output.stats || {},
      warnings: output.warnings || [],
      quota: null,
      checkingIds: new Set(),
      noteProgressEl: _reviewNoteProgressEl, // MSG-016：工具卡进度文字节点
    };
    state.focusIndex = -1;
    // 新一批 detections 到达：服务端账本清零，配额从满额开始（§6.3）
    api("/api/recheck/reset", { method: "POST" })
      .then(() => refreshQuota())
      .catch(() => refreshQuota());
    renderReviewStatic();
    renderEssay();
    renderAllCards();
    renderProgress();
    ovlReview.classList.add("open");
  }

  function closeReviewWorkbench() {
    ovlReview.classList.remove("open");
  }

  // ---------- 批阅稿（MSG-015：批改痕迹印在作文上，可打印） ----------
  async function openMarksheet() {
    if (!state.review) return;
    try {
      const resp = await requestFinalize();
      _marksheetResp = resp;
      renderMarksheet(resp);
      ovlMarksheet.classList.add("open");
    } catch (e) {
      toast(`生成批阅稿失败：${e.message}`);
    }
  }

  function closeMarksheet() {
    ovlMarksheet.classList.remove("open");
  }

  function renderMarksheet(resp) {
    const rv = state.review;
    const marks = resp.marks || [];
    // 作文：plain 原样 / removed 划掉 / added 标绿+上标编号
    let essayHtml = "";
    for (const m of marks) {
      if (m.kind === "plain") {
        essayHtml += escapeHtml(m.text);
      } else if (m.kind === "removed") {
        essayHtml += `<span class="ms-removed">${escapeHtml(m.text)}</span>`;
      } else if (m.kind === "added") {
        essayHtml += `<span class="ms-added">${escapeHtml(m.text)}</span><sup class="ms-sup">${m.index}</sup>`;
      }
    }
    // 讲解列表：按 index 顺序，用 detection_id 取 explanation_zh
    const kept = resp.kept || [];
    const detById = new Map(kept.map((d) => [d.id, d]));
    const addedMarks = marks.filter((m) => m.kind === "added").sort((a, b) => a.index - b.index);
    let exHtml = "";
    if (addedMarks.length) {
      exHtml = '<div class="ms-explanations"><h3>逐条讲解</h3>';
      for (const m of addedMarks) {
        const det = detById.get(m.detection_id);
        const zh = det?.explanation_zh || "";
        exHtml += `<div class="ms-ex-item"><span class="ms-ex-num">${m.index}.</span>${escapeHtml(zh || "（无讲解）")}</div>`;
      }
      exHtml += "</div>";
    }
    // warnings
    const warns = resp.warnings || [];
    marksheetWarn.textContent = warns.length ? `${warns.length} 处未能定位，未套用` : "";
    marksheetBody.innerHTML =
      `<div class="ms-essay">${essayHtml || "<i>（无改动）</i>"}</div>` +
      exHtml +
      (rv.summary ? `<div class="ms-summary"><h3>AI 总评</h3><p>${escapeHtml(rv.summary)}</p></div>` : "") +
      (rvComment.value.trim() ? <div class="ms-comment"><h3>整体评语</h3><p>${escapeHtml(rvComment.value.trim())}</p></div>` : "");
  }

  function renderReviewStatic() {
    const rv = state.review;
    const stats = rv.stats || {};
    rvWordcount.textContent = `${stats.words ?? rv.essay.split(/\s+/).filter(Boolean).length} words`;
    rvSumMeta.textContent = `grade_text · ${state.models.grade || "—"}`;
    rvSummaryText.textContent = rv.summary || "";
    let chips = `<span class="stat-chip"><b>${stats.words ?? "?"}</b>词</span>`;
    if (stats.error_count != null) chips += `<span class="stat-chip e"><b>${stats.error_count}</b>要改</span>`;
    if (stats.suggestion_count != null) chips += `<span class="stat-chip s"><b>${stats.suggestion_count}</b>建议</span>`;
    if (stats.total != null) chips += `<span class="stat-chip"><b>${stats.total}</b>合计</span>`;
    rvStats.innerHTML = chips;
    rvWarnings.innerHTML = (rv.warnings || [])
      .map(
        (w) =>
          `<span class="dc-hint"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>${escapeHtml(w)}</span>`
      )
      .join("");
  }

  // 左栏：原文 + 标注（error 红下划线 / suggestion 荧光黄 / withdrawn 灰删除线）
  function renderEssay() {
    const rv = state.review;
    const text = rv.essay || "";
    // aligned 且区间有效才能标注；按 start 升序排，跳过重叠区间
    const spans = rv.detections
      .filter(
        (d) =>
          d.aligned &&
          Number.isInteger(d.start) &&
          Number.isInteger(d.end) &&
          d.start >= 0 &&
          d.end > d.start &&
          d.end <= text.length
      )
      .sort((a, b) => a.start - b.start || b.end - a.end);
    let html = "";
    let cursor = 0;
    const used = [];
    for (const d of spans) {
      if (d.start < cursor) continue; // 与已插区间重叠，保守跳过
      const cls =
        d.recheck && d.recheck.status === "withdrawn"
          ? "mk mk-w"
          : d.tier === "suggestion"
            ? "mk mk-s"
            : "mk mk-e";
      html += escapeHtml(text.slice(cursor, d.start));
      html += `<span class="${cls}" data-mk="${escapeHtml(d.id)}">${escapeHtml(text.slice(d.start, d.end))}</span>`;
      cursor = d.end;
      used.push(d.id);
    }
    html += escapeHtml(text.slice(cursor));
    rvEssayText.innerHTML = html;
    bindMarkHover();
  }

  // 左右栏双向 hover 联动（G9）
  function bindMarkHover() {
    rvEssayText.querySelectorAll(".mk[data-mk]").forEach((mk) => {
      mk.addEventListener("mouseenter", () => {
        markHot(mk.dataset.mk, true);
        const card = rvCards.querySelector(`.dcard[data-dc="${CSS.escape(mk.dataset.mk)}"]`);
        if (card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
      });
      mk.addEventListener("mouseleave", () => markHot(mk.dataset.mk, false));
    });
  }

  function markHot(id, on) {
    rvEssayText.querySelector(`.mk[data-mk="${CSS.escape(id)}"]`)?.classList.toggle("hot", on);
    rvCards.querySelector(`.dcard[data-dc="${CSS.escape(id)}"]`)?.classList.toggle("hot", on);
  }

  // 配额（§6.2 / §6.4：账本在服务端，前端只展示与降级）
  async function refreshQuota() {
    const rv = state.review;
    if (!rv) return;
    try {
      rv.quota = await api("/api/recheck/quota");
    } catch {
      return;
    }
    const q = rv.quota;
    rvQuota.textContent = `复核余量 ${q.total_remaining}/${q.total_max}`;
    rvQuota.classList.toggle("low", q.total_remaining <= 2);
    // 配额变化影响驳回按钮文案与降级，全部待裁决卡重渲
    for (const d of rv.detections) {
      if (!rv.decisions[d.id] && !rv.checkingIds.has(d.id)) renderCard(d.id);
    }
    renderProgress();
  }

  function perItemRemaining(det) {
    const q = state.review?.quota;
    if (!q) return null;
    if (q.total_remaining <= 0) return 0;
    const used = q.per_item_used?.[det.id] ?? 0;
    return Math.max(q.per_item_max - used, 0);
  }

  function badgeRow(det) {
    const tierBadge =
      det.tier === "suggestion"
        ? '<span class="badge b-sug">建议</span>'
        : '<span class="badge b-err">要改</span>';
    const [typeLabel, typeCls] = TYPE_LABELS[det.type] || [det.type || "其他", "b-hard"];
    const typeBadge = `<span class="badge ${typeCls}">${escapeHtml(typeLabel)}</span>`;
    const [srcLabel, srcCls] = SOURCE_LABELS[det.source] || [null, null];
    const srcBadge = srcLabel ? `<span class="badge ${srcCls}">${escapeHtml(srcLabel)}</span>` : "";
    return `<span class="dc-id">${escapeHtml(det.id)}</span>${tierBadge}${typeBadge}${srcBadge}`;
  }

  const ARROW =
    '<svg class="dc-arrow" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5l7 7-7 7"/></svg>';

  // 复核回卡后的 note 条（upheld 绿 / revised 黄 / withdrawn 灰）
  function recheckNote(det) {
    const rc = det.recheck;
    if (!rc) return "";
    const roundText = `（第 ${rc.round} 轮复核）`;
    if (rc.status === "upheld") {
      return `<div class="dc-note"><span class="nt">已复核 · 维持原判</span>AI 复核后仍维持原判，请你再拍板。<span class="rz">复核理由：${escapeHtml(rc.reason_zh || "")} ${roundText}</span></div>`;
    }
    if (rc.status === "revised") {
      return `<div class="dc-note warned"><span class="nt">已复核 · 改判</span>AI 复核后给出了新改法，请你再拍板。<span class="rz">复核理由：${escapeHtml(rc.reason_zh || "")} ${roundText}</span></div>`;
    }
    if (rc.status === "withdrawn") {
      return `<div class="dc-note"><span class="nt">已撤销</span>AI 认可你的驳回，该条已撤销，不进定稿。<span class="rz">复核理由：${escapeHtml(rc.reason_zh || "")} ${roundText}</span></div>`;
    }
    return "";
  }

  function renderAllCards() {
    rvCards.innerHTML = "";
    for (const det of state.review.detections) {
      const card = el("div", "dcard");
      card.dataset.dc = det.id;
      bindCardHover(card);
      rvCards.appendChild(card);
      renderCardInto(card, det);
    }
  }

  function renderCard(id) {
    const det = state.review.detections.find((d) => d.id === id);
    const card = rvCards.querySelector(`.dcard[data-dc="${CSS.escape(id)}"]`);
    if (!det || !card) return;
    renderCardInto(card, det);
  }

  function renderCardInto(card, det) {
    const rv = state.review;
    const decision = rv.decisions[det.id];
    const checking = rv.checkingIds.has(det.id);
    const withdrawn = det.recheck && det.recheck.status === "withdrawn";
    card.className = "dcard";
    card.dataset.dc = det.id;

    let html = `<div class="dc-top">${badgeRow(det)}`;

    if (withdrawn) {
      // withdrawn：灰显 + 已撤销徽标 + 无操作按钮（decision 已置 rejected）
      card.classList.add("st-withdrawn");
      html += '<span class="badge b-wd">已撤销</span></div>';
      const neutralNew = det.corrected
        ? `<span class="dc-new neutral">${escapeHtml(det.corrected)}</span>`
        : "";
      html += `<div class="dc-fix"><span class="dc-old">${escapeHtml(det.original)}</span>${det.corrected ? ARROW : ""}${neutralNew}</div>`;
      html += recheckNote(det);
      card.innerHTML = html;
      return;
    }

    if (decision === "accepted") {
      card.classList.add("st-accepted");
      html += '</div>';
      html += `<div class="dc-fix"><span class="dc-old">${escapeHtml(det.original)}</span>${ARROW}<span class="dc-new">${escapeHtml(det.corrected)}</span></div>`;
      html += `<div class="dc-exp">${escapeHtml(det.explanation_zh || "")}</div>`;
      html += recheckNote(det);
      html += `<div class="dc-act"><span class="dc-state-tag ok"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg>已采纳</span><button class="btn btn-ghost btn-undo">撤销</button></div>`;
      card.innerHTML = html;
      card.querySelector(".btn-undo").addEventListener("click", () => undo(det.id));
      return;
    }

    if (decision === "edited") {
      card.classList.add("st-edited");
      html += '<span class="badge b-acc">改后采纳</span></div>';
      const override = rv.overrides[det.id] ?? det.corrected;
      html += `<div class="dc-fix"><span class="dc-old">${escapeHtml(det.original)}</span>${ARROW}<span class="dc-new">${escapeHtml(override)}</span></div>`;
      html += `<div class="dc-exp">${escapeHtml(det.explanation_zh || "")}</div>`;
      html += recheckNote(det);
      html += `<div class="dc-act"><span class="dc-state-tag acc"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>改后采纳</span><button class="btn btn-ghost btn-undo">撤销</button></div>`;
      card.innerHTML = html;
      card.querySelector(".btn-undo").addEventListener("click", () => undo(det.id));
      return;
    }

    // ---- 待裁决（含复核回卡与配额降级） ----
    html += "</div>";
    // dc-fix：revised 时三段（原文 → 旧改法删除线 → 新改法）
    const oldCorrected = det._oldCorrected;
    html += `<div class="dc-fix"><span class="dc-old">${escapeHtml(det.original)}</span>`;
    if (oldCorrected && oldCorrected !== det.corrected) {
      html += ARROW + `<span class="dc-old strike-soft">${escapeHtml(oldCorrected)}</span>`;
    }
    html += ARROW + `<span class="dc-new">${escapeHtml(det.corrected || "")}</span></div>`;
    html += `<div class="dc-exp">${escapeHtml(det.explanation_zh || "")}</div>`;
    html += recheckNote(det);

    if (checking) {
      card.classList.add("st-checking");
      html += `<div class="dc-act"><span class="dc-state-tag acc"><span class="sc-spin"></span>已发起复核，请稍候…</span></div>`;
      card.innerHTML = html;
      return;
    }

    const remain = perItemRemaining(det);
    const exhausted = remain != null && remain <= 0;
    if (exhausted) {
      html += `<div class="dc-hint warn"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>该条已复核 ${rv.quota?.per_item_max ?? 2} 次：再次驳回将不发复核，直接从结果中剔除。</div>`;
    }
    const rejectLabel = exhausted ? "剔除此条" : `驳回 · 还可复核 ${remain ?? "?"} 次`;
    html += `<div class="dc-act">
      <button class="btn btn-pri">采纳 <b>A</b></button>
      <button class="btn">修改 <b>E</b></button>
      <button class="btn btn-danger">${escapeHtml(rejectLabel)} <b>D</b></button>
    </div>`;
    card.innerHTML = html;
    const [acceptBtn, editBtn, rejectBtn] = card.querySelectorAll(".dc-act .btn");
    acceptBtn.addEventListener("click", () => decide(det.id, "accepted"));
    editBtn.addEventListener("click", () => startEdit(card, det));
    rejectBtn.addEventListener("click", () => reject(det.id));
  }

  // 修改（E）：卡内 inline 编辑表单
  function startEdit(card, det) {
    const act = card.querySelector(".dc-act");
    if (!act || card.querySelector(".dc-edit")) return;
    const form = el("div", "dc-edit");
    form.innerHTML = `<input type="text" value="${escapeHtml(det.corrected || "")}" placeholder="写出你的改法" /><div class="dc-edit-row"><button class="btn btn-pri">保存</button><button class="btn btn-ghost">取消</button></div>`;
    act.replaceWith(form);
    const inputEl = form.querySelector("input");
    inputEl.focus();
    inputEl.select();
    form.querySelector(".btn-pri").addEventListener("click", () => {
      const val = inputEl.value.trim();
      if (!val) return toast("改法不能为空");
      state.review.overrides[det.id] = val;
      decide(det.id, "edited");
    });
    form.querySelector(".btn-ghost").addEventListener("click", () => renderCard(det.id));
    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        form.querySelector(".btn-pri").click();
      }
      if (e.key === "Escape") {
        e.stopPropagation();
        renderCard(det.id);
      }
    });
  }

  function decide(id, kind) {
    const rv = state.review;
    if (!rv) return;
    rv.decisions[id] = kind;
    renderCard(id);
    renderProgress();
    renderPreview();
  }

  function undo(id) {
    const rv = state.review;
    if (!rv) return;
    delete rv.decisions[id];
    delete rv.overrides[id];
    renderCard(id);
    renderProgress();
    renderPreview();
  }

  // 驳回（D）：配额内发 run3 复核；用尽则直接剔除（§6.4）
  function reject(id) {
    const rv = state.review;
    if (!rv) return;
    const det = rv.detections.find((d) => d.id === id);
    if (!det) return;
    const remain = perItemRemaining(det);
    if (remain != null && remain <= 0) {
      rv.decisions[id] = "rejected";
      renderCard(id);
      renderProgress();
      renderPreview();
      toast(`已复核 ${rv.quota.per_item_max} 次，本次直接从结果中剔除`);
      return;
    }
    if (state.running) return toast("当前有任务正在执行，请稍候");
    rv.checkingIds.add(id);
    renderCard(id);
    // 反谄媚纪律：中性写法，禁写「驳回 / 认为你错了」
    runChat(`请复核这条批改意见：${det.id} / ${det.original}`);
  }

  // 复核结果回卡（MSG-010 ②：按 follow_up 驱动，id 原地替换、绝不新建卡片）
  function applyRecheckResult(output) {
    const rv = state.review;
    if (!rv) return;
    const idx = rv.detections.findIndex((d) => d.id === output.id);
    if (idx < 0) {
      toast(`复核结果找不到对应条目（${output.id}）`);
      return;
    }
    const old = rv.detections[idx];
    const next = output.detection || old;
    if (output.follow_up === "terminal") {
      // withdrawn：灰显禁操作，decision 置 rejected
      rv.decisions[output.id] = "rejected";
    } else {
      // upheld / revised：回到待裁决，decision 清空
      delete rv.decisions[output.id];
      delete rv.overrides[output.id];
      if (output.status === "revised" && old.corrected && old.corrected !== next.corrected) {
        next._oldCorrected = old.corrected; // 旧改法删除线展示
      }
    }
    rv.detections[idx] = next;
    rv.checkingIds.delete(output.id);
    renderEssay();
    renderCard(output.id);
    renderProgress();
    renderPreview();
    refreshQuota();
    if (ovlReview.classList.contains("open")) {
      const card = rvCards.querySelector(`.dcard[data-dc="${CSS.escape(output.id)}"]`);
      card?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  function recheckFailed(ev) {
    const rv = state.review;
    toast(ev.error || "复核失败，请稍后再试");
    if (!rv) return;
    rv.checkingIds.clear();
    for (const det of rv.detections) {
      if (!rv.decisions[det.id]) renderCard(det.id);
    }
    refreshQuota(); // 失败不计数，拉一次权威配额
  }

  function renderProgress() {
    const rv = state.review;
    if (!rv) return;
    const total = rv.detections.length;
    const done = rv.detections.filter((d) => rv.decisions[d.id] != null).length;
    rvProgFill.style.transform = `scaleX(${total ? done / total : 0})`;
    rvProgText.textContent = `已处理 ${done} / ${total}`;
    const keptCount = rv.detections.filter(
      (d) => rv.decisions[d.id] === "accepted" || rv.decisions[d.id] === "edited"
    ).length;
    rvCopyNote.textContent = `复制将包含 ${keptCount} 条已确认修改 + 整体评语`;

    // MSG-016：工具卡 note 跟着裁决进度变三态（纯前端，不进账本不调模型）
    if (rv.noteProgressEl) {
      let text;
      if (done === 0) {
        text = "已打开「批改裁决工作台」，逐条确认后生成定稿 →";
      } else if (done < total) {
        text = `已确认 ${done} 条，还有 ${total - done} 条没判 · 继续裁决 →`;
      } else {
        text = `已采纳 ${keptCount} 条、驳回 ${done - keptCount} 条，已生成定稿 · 再看一眼 →`;
      }
      rv.noteProgressEl.textContent = text;
    }
  }

  // 预览与复制的共用拼接（本地实时版；复制时另走服务端 apply_decisions 定稿）
  function buildResultText() {
    const rv = state.review;
    if (!rv) return "";
    const kept = rv.detections.filter(
      (d) => rv.decisions[d.id] === "accepted" || rv.decisions[d.id] === "edited"
    );
    const lines = ["【批改结果】"];
    kept.forEach((d, i) => {
      const edited = rv.decisions[d.id] === "edited";
      const fixed = edited ? (rv.overrides[d.id] ?? d.corrected) : d.corrected;
      lines.push(`${i + 1}. ${d.original} → ${fixed}${edited ? "（手动修改）" : ""}`);
      if (d.explanation_zh) lines.push(`   讲解：${d.explanation_zh}`);
    });
    if (!kept.length) lines.push("本轮没有保留任何修改。");
    const comment = rvComment.value.trim();
    if (comment) {
      lines.push("");
      lines.push(`整体评语：${comment}`);
    }
    return lines.join("\n");
  }

  function renderPreview() {
    if (!state.review) return;
    rvPreviewPre.textContent = buildResultText();
  }

  async function copyResult() {
    const rv = state.review;
    if (!rv) return;
    try {
      const resp = await requestFinalize();
      const text = resp.result_text || "";
      rvPreviewPre.textContent = text; // 先填预览：剪贴板失败时用户仍可手动复制
      const warnCount = (resp.warnings || []).length;
      try {
        await copyToClipboard(text);
        toast(`已复制 ${(resp.kept || []).length} 条修改 + 评语${warnCount ? `（${warnCount} 条提示见左栏）` : ""}`, true);
      } catch {
        toast("剪贴板写入失败，定稿文本已放在预览区，可手动复制");
      }
      showFinalizeWarnings(resp.warnings || []);
    } catch (e) {
      toast(`定稿失败：${e.message}`);
    }
  }

  // MSG-015 第3步：复制改后全文（后端 finalize_text 已把改动套回原文）
  async function copyFinalText() {
    const rv = state.review;
    if (!rv) return;
    try {
      const resp = await requestFinalize();
      const text = resp.final_text || "";
      rvPreviewPre.textContent = text; // 先填预览：剪贴板失败时用户仍可手动复制
      try {
        await copyToClipboard(text);
        toast(`已复制改后全文（${(resp.kept || []).length} 条改动已套用）`, true);
      } catch {
        toast("剪贴板写入失败，改后全文已放在预览区，可手动复制");
      }
      showFinalizeWarnings(resp.warnings || []);
    } catch (e) {
      toast(`定稿失败：${e.message}`);
    }
  }

  // 公共：调 /api/grade/finalize，返回 {final_text, result_text, marks, kept, warnings}
  // （纯函数端点，模型不在场）
  async function requestFinalize() {
    const rv = state.review;
    const decisions = { ...rv.decisions };
    const overrides = Object.keys(rv.overrides).length ? { ...rv.overrides } : null;
    const edited_ids = Object.keys(rv.decisions).filter(
      (id) => rv.decisions[id] === "edited"
    );
    const resp = await api("/api/grade/finalize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: rv.essay,
        detections: rv.detections.map((d) => {
          const copy = { ...d };
          delete copy._oldCorrected; // 前端展示字段不回传
          return copy;
        }),
        decisions,
        overrides,
        comment: rvComment.value.trim(),
        edited_ids,
      }),
    });
    return resp;
  }

  // 定稿 warnings 上界面（MSG-015：不能只 console.warn，那是静默错误）
  function showFinalizeWarnings(warnings) {
    if (!warnings || !warnings.length) {
      if (rvWarnings) rvWarnings.innerHTML = "";
      return;
    }
    if (rvWarnings) {
      rvWarnings.innerHTML = warnings
        .map(
          (w) =>
            `<span class="dc-hint"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>${escapeHtml(w)}</span>`
        )
        .join("");
    }
  }

  // 剪贴板写入：clipboard API 要求页面聚焦（后台页/自动化会抛 not focused），失败回退 execCommand
  async function copyToClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      if (!ok) throw new Error("clipboard unavailable");
    }
  }

  function acceptAll() {
    const rv = state.review;
    if (!rv) return;
    for (const d of rv.detections) {
      if (!rv.decisions[d.id] && !rv.checkingIds.has(d.id)) {
        if (d.recheck && d.recheck.status === "withdrawn") continue; // 已撤销的保持剔除
        rv.decisions[d.id] = "accepted";
      }
    }
    renderAllCards();
    renderProgress();
    renderPreview();
  }

  function bindCardHover(card) {
    card.addEventListener("mouseenter", () => markHot(card.dataset.dc, true));
    card.addEventListener("mouseleave", () => markHot(card.dataset.dc, false));
  }

  // ---------- 快捷键（MSG-008：A/E/D/J/K/↑↓/Esc） ----------
  function focusCard(delta) {
    const cards = [...rvCards.querySelectorAll(".dcard")];
    if (!cards.length) return;
    state.focusIndex = Math.min(Math.max(state.focusIndex + delta, 0), cards.length - 1);
    cards.forEach((c, i) => c.classList.toggle("dc-focused", i === state.focusIndex));
    cards[state.focusIndex].scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function focusDecision(kind) {
    const rv = state.review;
    if (!rv) return;
    const cards = [...rvCards.querySelectorAll(".dcard")];
    if (!cards.length) return;
    if (state.focusIndex < 0) {
      focusCard(1);
      return;
    }
    const id = cards[state.focusIndex].dataset.dc;
    const det = rv.detections.find((d) => d.id === id);
    if (!det) return;
    if (rv.checkingIds.has(id)) return;
    if (det.recheck && det.recheck.status === "withdrawn") return;
    if (rv.decisions[id]) {
      if (kind === "undo") undo(id);
      return;
    }
    if (kind === "accepted") decide(id, "accepted");
    else if (kind === "edited") startEdit(cards[state.focusIndex], det);
    else if (kind === "rejected") reject(id);
  }

  document.addEventListener("keydown", (e) => {
    const ae = document.activeElement;
    const editing = !!ae && (["INPUT", "TEXTAREA"].includes(ae.tagName) || ae.isContentEditable);
    // 藏在已关闭遮罩里的残留焦点（如 OCR 台关闭后仍持有焦点的 contenteditable）不算"正在输入"
    const hidden = !!ae && !ovlReview.contains(ae) && !ae.offsetParent;
    const inField = editing && !hidden;
    if (e.key === "Escape") {
      if (modelPop.classList.contains("open")) {
        modelPop.classList.remove("open");
        return;
      }
      if (ovlOcr.classList.contains("open")) {
        closeOcrWorkbench();
        return;
      }
      if (ovlMarksheet.classList.contains("open")) {
        closeMarksheet();
        return;
      }
      if (ovlReview.classList.contains("open")) {
        closeReviewWorkbench();
        return;
      }
      return;
    }
    if (!ovlReview.classList.contains("open") || ovlMarksheet.classList.contains("open") || inField) return;
    switch (e.key.toLowerCase()) {
      case "a": focusDecision("accepted"); break;
      case "e": focusDecision("edited"); break;
      case "d": focusDecision("rejected"); break;
      case "j": case "arrowdown": focusCard(1); break;
      case "k": case "arrowup": focusCard(-1); break;
    }
  });

  // ---------- 工具卡里「重新打开工作台」入口 ----------
  chatInner.addEventListener("click", (e) => {
    const wb = e.target.closest(".tc-open-wb");
    if (!wb) return;
    if (wb.dataset.wb === "ocr" && state.ocr) ovlOcr.classList.add("open");
    if (wb.dataset.wb === "review" && state.review) {
      ovlReview.classList.add("open");
      renderEssay();
    }
  });

  // ---------- 模型信息弹出 ----------
  function bindModelPop() {
    $("btn-model").addEventListener("click", (e) => {
      e.stopPropagation();
      modelPop.classList.toggle("open");
      $("btn-model").querySelector(".mb-chev").style.transform = modelPop.classList.contains("open")
        ? "rotate(180deg)"
        : "";
    });
    document.addEventListener("click", (e) => {
      if (
        modelPop.classList.contains("open") &&
        !modelPop.contains(e.target) &&
        !$("btn-model").contains(e.target)
      ) {
        modelPop.classList.remove("open");
        $("btn-model").querySelector(".mb-chev").style.transform = "";
      }
    });
  }

  // ---------- 初始化 ----------
  async function loadTools() {
    try {
      const data = await api("/api/tools");
      state.models = data.models || {};
      const count = (data.tools || []).length;
      $("tool-count").textContent = count ? `${count} tools` : "";
      modelTagText.textContent = state.models.chat || "Markr";
      $("mp-chat").textContent = state.models.chat || "—";
      $("mp-grade").textContent = state.models.grade || "—";
      $("mp-vision").textContent = state.models.vision || "—";
    } catch {
      modelTagText.textContent = "Markr";
    }
  }

  function init() {
    // 会话侧栏
    $("btn-new-session").addEventListener("click", createSession);
    $("btn-clear").addEventListener("click", createSession);
    $("btn-sidebar-open").addEventListener("click", openSidebar);
    sidebarBackdrop.addEventListener("click", closeSidebar);
    $("btn-batch").addEventListener("click", toggleBatchMode);
    $("btn-batch-del").addEventListener("click", batchDelete);
    $("select-all-cb").addEventListener("change", (e) => {
      const on = e.target.checked;
      state.selectedIds = new Set(on ? state.sessions.map((s) => s.session_id) : []);
      $("btn-batch-del").disabled = !on;
      renderSessions();
    });

    // 欢迎建议
    document.querySelectorAll(".suggestion").forEach((btn) => {
      btn.addEventListener("click", () => {
        input.value = btn.dataset.prompt;
        input.focus();
        input.dispatchEvent(new Event("input"));
      });
    });

    // 输入自适应
    input.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 160) + "px";
    });

    // 发送 / 停止
    sendBtn.addEventListener("click", onSendOrStop);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        onSendOrStop();
      }
    });

    // 上传
    $("btn-upload").addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files?.[0];
      if (file) await uploadFile(file);
      fileInput.value = "";
    });

    // OCR 台
    $("btn-ocr-close").addEventListener("click", closeOcrWorkbench);
    $("btn-ocr-confirm").addEventListener("click", confirmOcr);
    $("btn-ocr-retry").addEventListener("click", retryOcr);

    // 裁决台
    $("btn-review-close").addEventListener("click", closeReviewWorkbench);
    $("btn-rv-copy").addEventListener("click", copyResult);
    $("btn-rv-copy-final").addEventListener("click", copyFinalText);
    $("btn-rv-marksheet").addEventListener("click", openMarksheet);
    $("btn-rv-accept-all").addEventListener("click", acceptAll);
    // 批阅稿浮层
    $("btn-marksheet-close").addEventListener("click", closeMarksheet);
    $("btn-marksheet-print").addEventListener("click", () => window.print());
    $("btn-marksheet-copy-list").addEventListener("click", () => {
      if (!_marksheetResp) return;
      copyToClipboard(_marksheetResp.result_text || "").then(
        () => toast("已复制批改清单", true),
        () => toast("剪贴板写入失败")
      );
    });
    $("btn-marksheet-copy-final").addEventListener("click", () => {
      if (!_marksheetResp) return;
      copyToClipboard(_marksheetResp.final_text || "").then(
        () => toast("已复制改后全文", true),
        () => toast("剪贴板写入失败")
      );
    });
    rvComment.addEventListener("input", renderPreview);

    bindModelPop();
    loadTools();
    loadSessions();
  }

  init();
})();
