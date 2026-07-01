// JARVIS overlay front-end. Talks to Python via window.pywebview.api.*;
// Python calls back into the functions declared here (top-level, so they're
// globals) via window.evaluate_js().

const transcript = document.getElementById("transcript");
const statusEl = document.getElementById("status");
const entry = document.getElementById("entry");
const sendBtn = document.getElementById("send-btn");
const micBtn = document.getElementById("mic-btn");
const speakBtn = document.getElementById("speak-btn");
const clearBtn = document.getElementById("clear-btn");
const closeBtn = document.getElementById("close-btn");
const header = document.getElementById("header");

let userName = "User";
let streamEl = null;
let streamBuffer = "";

const THINKING_HTML = `
    <span class="thinking-dots"><i></i><i></i><i></i></span>
    <span class="thinking-label">JARVIS is thinking&hellip;</span>
  `;

// ── API bridge ────────────────────────────────────────────────────────────

function callApi(method, ...args) {
  if (window.pywebview && window.pywebview.api) {
    window.pywebview.api[method](...args);
  }
}

// Like callApi but returns the Python method's value (pywebview resolves a
// promise). Used by the voice-dials panel to read/refresh state.
function callApiAsync(method, ...args) {
  if (window.pywebview && window.pywebview.api) {
    return window.pywebview.api[method](...args);
  }
  return Promise.resolve(null);
}

// ── Markdown (mirrors app/overlay.py's previous tkinter renderer) ──────────

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const INLINE_RE = /(\*\*[^*\n]+\*\*|\*[^*\n]+\*|`[^`\n]+`)/;

function renderInline(line) {
  return line
    .split(INLINE_RE)
    .map((part) => {
      if (!part) return "";
      if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
        return `<b>${part.slice(2, -2)}</b>`;
      }
      if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
        return `<i>${part.slice(1, -1)}</i>`;
      }
      if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
        return `<span class="md-code">${part.slice(1, -1)}</span>`;
      }
      return part;
    })
    .join("");
}

function isTableRow(line) {
  const t = line.trim();
  return t.startsWith("|") || (t.includes("|") && !t.startsWith("```"));
}

function isTableSeparator(line) {
  return /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$/.test(line.trim());
}

function splitTableRow(line) {
  let t = line.trim();
  if (t.startsWith("|")) t = t.slice(1);
  if (t.endsWith("|")) t = t.slice(0, -1);
  return t.split("|").map((cell) => cell.trim());
}

function renderTable(headerCells, bodyRows) {
  let html = `<table class="md-table"><thead><tr>`;
  for (const cell of headerCells) {
    html += `<th>${renderInline(escapeHtml(cell))}</th>`;
  }
  html += `</tr></thead><tbody>`;
  for (const row of bodyRows) {
    html += `<tr>`;
    for (const cell of row) {
      html += `<td>${renderInline(escapeHtml(cell))}</td>`;
    }
    html += `</tr>`;
  }
  html += `</tbody></table>`;
  return html;
}

function renderMarkdown(text) {
  const lines = text.replace(/\n+$/, "").split("\n");
  let html = "";
  let inCodeBlock = false;

  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    const line = escapeHtml(raw);

    if (line.startsWith("```")) {
      inCodeBlock = !inCodeBlock;
      continue;
    }
    if (inCodeBlock) {
      html += `<div class="md-code-block">${line || "&nbsp;"}</div>`;
      continue;
    }
    if (
      isTableRow(raw) &&
      i + 1 < lines.length &&
      isTableSeparator(lines[i + 1])
    ) {
      const headerCells = splitTableRow(raw);
      let j = i + 2;
      const bodyRows = [];
      while (j < lines.length && isTableRow(lines[j]) && !isTableSeparator(lines[j])) {
        bodyRows.push(splitTableRow(lines[j]));
        j++;
      }
      html += renderTable(headerCells, bodyRows);
      i = j - 1;
      continue;
    }
    if (line.startsWith("### ")) {
      html += `<div class="md-h3">${renderInline(line.slice(4))}</div>`;
    } else if (line.startsWith("## ")) {
      html += `<div class="md-h2">${renderInline(line.slice(3))}</div>`;
    } else if (line.startsWith("# ")) {
      html += `<div class="md-h1">${renderInline(line.slice(2))}</div>`;
    } else if (/^[-*] /.test(line)) {
      html += `<div class="md-line"><span class="md-bullet">&bull;</span>${renderInline(line.slice(2))}</div>`;
    } else if (/^\d+\. /.test(line)) {
      const m = line.match(/^(\d+\.) (.*)/);
      html += `<div class="md-line"><span class="md-bullet">${m[1]}</span>${renderInline(m[2])}</div>`;
    } else {
      html += `<div class="md-line">${renderInline(line) || "&nbsp;"}</div>`;
    }
  }
  return html;
}

// ── Transcript rendering (called from Python) ──────────────────────────────

function scrollToBottom() {
  transcript.scrollTop = transcript.scrollHeight;
}

function setUserName(name) {
  userName = name;
}

// ── Copy-to-clipboard (for grabbing emails, drafts, etc.) ──────────────────

const COPY_ICON = "&#128203;"; // 📋
const CHECK_ICON = "&#10003;"; // ✓

async function copyText(text, btn) {
  let ok = false;
  // Async Clipboard API works in the embedded WebView under a user gesture.
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (_) {
    /* fall through to legacy path */
  }
  if (!ok) {
    // Legacy fallback for webviews without async clipboard access.
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand("copy");
      document.body.removeChild(ta);
    } catch (_) {
      /* nothing more we can do */
    }
  }
  if (btn) {
    btn.classList.add("copied");
    btn.innerHTML = ok ? CHECK_ICON : "&#33;"; // ✓ or ! on failure
    btn.title = ok ? "Copied!" : "Copy failed";
    setTimeout(() => {
      btn.classList.remove("copied");
      btn.innerHTML = COPY_ICON;
      btn.title = "Copy";
    }, 1200);
  }
}

// Add a hover-reveal copy button that grabs the message's clean visible text
// (innerText strips markdown markers, so pasted emails/drafts come out tidy).
function attachCopyButton(wrap, contentEl) {
  const btn = document.createElement("button");
  btn.className = "msg-copy";
  btn.title = "Copy";
  btn.innerHTML = COPY_ICON;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    copyText(contentEl.innerText, btn);
  });
  wrap.appendChild(btn);
}

function addMessage(role, label, text) {
  document.body.classList.add("has-messages");

  const wrap = document.createElement("div");
  wrap.className = `msg msg-${role}`;

  const labelEl = document.createElement("div");
  labelEl.className = "msg-label";
  labelEl.textContent = label;
  wrap.appendChild(labelEl);

  const content = document.createElement("div");
  content.className = "msg-content";
  if (role === "assistant") {
    content.innerHTML = renderMarkdown(text);
  } else {
    content.textContent = text;
  }
  wrap.appendChild(content);

  if (role !== "system") {
    attachCopyButton(wrap, content);
  }

  transcript.appendChild(wrap);
  scrollToBottom();
}

function startAssistantMessage() {
  document.body.classList.add("has-messages");

  const wrap = document.createElement("div");
  wrap.className = "msg msg-assistant";

  const labelEl = document.createElement("div");
  labelEl.className = "msg-label";
  labelEl.textContent = "JARVIS";
  wrap.appendChild(labelEl);

  const content = document.createElement("div");
  content.className = "msg-content thinking";
  content.innerHTML = THINKING_HTML;
  wrap.appendChild(content);

  transcript.appendChild(wrap);
  streamEl = content;
  streamBuffer = "";
  scrollToBottom();
}

// Append one streamed text chunk and re-render from the accumulated buffer
// (cheap at chat length, and re-parsing the whole buffer means a markdown
// marker split across two chunks still renders correctly once complete).
// The first chunk of a round replaces the thinking dots with live text.
function appendAssistantDelta(chunk) {
  if (!streamEl) return;
  streamEl.classList.remove("thinking");
  streamBuffer += chunk;
  streamEl.innerHTML = renderMarkdown(streamBuffer);
  scrollToBottom();
}

// Claude sometimes narrates before deciding to call a tool ("Let me check
// that...") — that text isn't the real answer, so the backend discards it and
// calls this to roll the live stream back to the thinking state before the
// next round's text starts arriving.
function resetAssistantStream() {
  if (!streamEl) return;
  streamBuffer = "";
  streamEl.classList.add("thinking");
  streamEl.innerHTML = THINKING_HTML;
}

// Finalize the reply. If it streamed in live, the text is already on screen —
// just swap to the fully-rendered markdown as a safety net (formatting can
// differ slightly once the trailing chunk lands) and attach the copy button.
// If nothing streamed (e.g. a very fast/tool-only round), fall back to the
// original stagger fade-in so the reply doesn't just pop in unstyled.
function finishAssistantMessage(fullText) {
  if (!streamEl) return;
  const el = streamEl;
  streamEl = null;
  const didStream = streamBuffer.length > 0;
  streamBuffer = "";

  el.classList.remove("thinking");
  el.innerHTML = renderMarkdown(fullText);
  attachCopyButton(el.parentElement, el);

  if (!didStream) {
    const blocks = Array.from(el.children);
    blocks.forEach((block, i) => {
      block.style.animationDelay = i * 60 + "ms";
      block.classList.add("reveal");
    });
  }

  // Keep the view pinned to the newest content as it settles in.
  scrollToBottom();
  setTimeout(scrollToBottom, 140);
  setTimeout(scrollToBottom, 400);
}

function clearTranscript() {
  transcript.querySelectorAll(".msg").forEach((el) => el.remove());
  document.body.classList.remove("has-messages");
  // A session reset restores the default dials; refresh the panel if it's open.
  if (dialsPanel && !dialsPanel.classList.contains("hidden")) {
    callApiAsync("get_dials").then(renderDials);
  }
}

function resetEntry() {
  entry.value = "";
  autoResize();
}

function setStatus(text) {
  statusEl.textContent = text;
}

function setState(state) {
  document.body.dataset.state = state;
  particles.setMode(state);
}

function setInputsEnabled(enabled) {
  entry.disabled = !enabled;
  sendBtn.disabled = !enabled;
}

function setRecording(recording) {
  micBtn.classList.toggle("recording", recording);
  micBtn.innerHTML = recording ? "&#9209;" : "&#127908;";
}

function setVoiceAvailable(available) {
  micBtn.disabled = !available;
}

// Speaker (TTS) toggle — filled speaker + cyan glow when on, muted when off.
function setTTSEnabled(enabled) {
  speakBtn.classList.toggle("active", enabled);
  speakBtn.innerHTML = enabled ? "&#128266;" : "&#128263;";
  speakBtn.title = enabled ? "Speaking replies (click to mute)" : "Speak replies";
}

function setTtsAvailable(available) {
  speakBtn.disabled = !available;
}

// ── Input handling ───────────────────────────────────────────────────────

function autoResize() {
  entry.style.height = "auto";
  entry.style.height = Math.min(entry.scrollHeight, 112) + "px";
}

function sendMessage() {
  const text = entry.value.trim();
  if (!text) return;
  entry.value = "";
  autoResize();
  callApi("send_message", text);
}

entry.addEventListener("input", autoResize);
entry.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
sendBtn.addEventListener("click", sendMessage);
micBtn.addEventListener("click", () => callApi("toggle_recording"));
speakBtn.addEventListener("click", () => callApi("toggle_tts"));
clearBtn.addEventListener("click", () => callApi("clear_chat"));
closeBtn.addEventListener("click", () => callApi("close_overlay"));

// ── Voice dials panel ──────────────────────────────────────────────────────
// Sliders mutate JARVIS's persona directly through Python (no model call, so
// no tokens). The next message just picks up the new values.

const dialsBtn = document.getElementById("dials-btn");
const dialsPanel = document.getElementById("dials-panel");
const dialsList = document.getElementById("dials-list");
const dialsSave = document.getElementById("dials-save");
const dialsReset = document.getElementById("dials-reset");
const dialsClose = document.getElementById("dials-close");

function renderDials(dials) {
  if (!Array.isArray(dials)) return;
  dialsList.innerHTML = "";
  for (const d of dials) {
    const row = document.createElement("div");
    row.className = "dial-row";
    row.dataset.key = d.key;
    row.innerHTML = `
      <div class="dial-top">
        <span class="dial-name">${escapeHtml(d.label)}</span>
        <span class="dial-value">${d.value}</span>
      </div>
      <input class="dial-range" type="range" min="0" max="100" step="5" value="${d.value}" />
      <div class="dial-desc">${escapeHtml(d.description)}</div>`;
    const range = row.querySelector(".dial-range");
    const valEl = row.querySelector(".dial-value");

    // Native range dragging breaks under `user-select: none` in the embedded
    // WebView (a click registers, a drag doesn't), so drive the thumb ourselves
    // with pointer capture — reliable regardless of that quirk.
    const valueFromX = (clientX) => {
      const rect = range.getBoundingClientRect();
      const pct = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
      return Math.round((pct * 100) / 5) * 5;
    };
    const preview = (v) => {
      range.value = v;
      valEl.textContent = v;
    };
    const commit = async (v) =>
      renderDials(await callApiAsync("set_dial", d.key, v));

    let dragging = false;
    range.addEventListener("pointerdown", (e) => {
      dragging = true;
      range.setPointerCapture(e.pointerId);
      preview(valueFromX(e.clientX));
      range.focus(); // preventDefault below would otherwise suppress focus
      e.preventDefault();
    });
    range.addEventListener("pointermove", (e) => {
      if (dragging) preview(valueFromX(e.clientX));
    });
    const finishDrag = (e) => {
      if (!dragging) return;
      dragging = false;
      try {
        range.releasePointerCapture(e.pointerId);
      } catch (_) {}
      commit(parseInt(range.value, 10));
    };
    range.addEventListener("pointerup", finishDrag);
    range.addEventListener("pointercancel", finishDrag);
    // Keyboard (arrow keys) still works through the native events.
    range.addEventListener("input", () => {
      valEl.textContent = range.value;
    });
    range.addEventListener("change", () => commit(parseInt(range.value, 10)));
    dialsList.appendChild(row);
  }
}

async function openDials() {
  closeSettings();
  const dials = await callApiAsync("get_dials");
  renderDials(dials);
  dialsPanel.classList.remove("hidden");
  dialsPanel.setAttribute("aria-hidden", "false");
}

function closeDials() {
  dialsPanel.classList.add("hidden");
  dialsPanel.setAttribute("aria-hidden", "true");
}

function toggleDials() {
  if (dialsPanel.classList.contains("hidden")) {
    openDials();
  } else {
    closeDials();
  }
}

dialsBtn.addEventListener("click", toggleDials);
dialsClose.addEventListener("click", closeDials);
dialsReset.addEventListener("click", async () => {
  renderDials(await callApiAsync("reset_dials"));
});
dialsSave.addEventListener("click", async () => {
  renderDials(await callApiAsync("save_dials_default"));
  dialsSave.classList.add("flash");
  setTimeout(() => dialsSave.classList.remove("flash"), 600);
});

// ── Settings panel ─────────────────────────────────────────────────────────
// System status (read-only) + editable note/task categories. Edits go straight
// to Python (saved to jarvis_config.json); the model's tools pick up new
// categories on restart.

const settingsBtn = document.getElementById("settings-btn");
const settingsPanel = document.getElementById("settings-panel");
const settingsSave = document.getElementById("settings-save");
const settingsClose = document.getElementById("settings-close");
const catsList = document.getElementById("cats-list");
const catsAddInput = document.getElementById("cats-add-input");
const catsAddBtn = document.getElementById("cats-add-btn");
const diagList = document.getElementById("diag-list");

function addCategoryRow(name) {
  const row = document.createElement("div");
  row.className = "cat-row";
  const input = document.createElement("input");
  input.className = "cat-input";
  input.type = "text";
  input.value = name;
  const remove = document.createElement("button");
  remove.className = "cat-remove";
  remove.title = "Remove";
  remove.innerHTML = "&#10005;";
  remove.addEventListener("click", () => row.remove());
  row.appendChild(input);
  row.appendChild(remove);
  catsList.appendChild(row);
}

function collectCategories() {
  return [...catsList.querySelectorAll(".cat-input")]
    .map((i) => i.value.trim())
    .filter(Boolean);
}

function renderDiagnostics(diags) {
  diagList.innerHTML = "";
  if (!Array.isArray(diags)) return;
  for (const d of diags) {
    const row = document.createElement("div");
    row.className = "diag-row";
    const badge = document.createElement("span");
    badge.className = "diag-badge" + (d.ok ? " on" : "");
    badge.textContent = d.ok ? "ON" : "off";
    const name = document.createElement("span");
    name.className = "diag-name";
    name.textContent = d.name;
    const detail = document.createElement("span");
    detail.className = "diag-detail";
    detail.textContent = d.detail;
    row.append(badge, name, detail);
    diagList.appendChild(row);
  }
}

function applySettings(s) {
  if (!s) return;
  catsList.innerHTML = "";
  (s.categories || []).forEach(addCategoryRow);
  renderDiagnostics(s.diagnostics);
  if (s.error) setStatus(`Settings: ${s.error}`);
}

async function openSettings() {
  closeDials();
  applySettings(await callApiAsync("get_settings"));
  settingsPanel.classList.remove("hidden");
  settingsPanel.setAttribute("aria-hidden", "false");
}

function closeSettings() {
  settingsPanel.classList.add("hidden");
  settingsPanel.setAttribute("aria-hidden", "true");
}

function toggleSettings() {
  if (settingsPanel.classList.contains("hidden")) {
    openSettings();
  } else {
    closeSettings();
  }
}

settingsBtn.addEventListener("click", toggleSettings);
settingsClose.addEventListener("click", closeSettings);
catsAddBtn.addEventListener("click", () => {
  const v = catsAddInput.value.trim();
  if (!v) return;
  addCategoryRow(v);
  catsAddInput.value = "";
  catsAddInput.focus();
});
catsAddInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    catsAddBtn.click();
  }
});
settingsSave.addEventListener("click", async () => {
  const res = await callApiAsync("save_categories", collectCategories());
  applySettings(res);
  if (!res || !res.error) {
    settingsSave.classList.add("flash");
    setTimeout(() => settingsSave.classList.remove("flash"), 600);
  }
});

// ── Window dragging (frameless window — no native title bar) ──────────────

let dragging = false;
let lastScreenX = 0;
let lastScreenY = 0;

header.addEventListener("mousedown", (e) => {
  if (e.target.closest("button")) return;
  dragging = true;
  lastScreenX = e.screenX;
  lastScreenY = e.screenY;
});
window.addEventListener("mousemove", (e) => {
  if (!dragging) return;
  const dx = e.screenX - lastScreenX;
  const dy = e.screenY - lastScreenY;
  lastScreenX = e.screenX;
  lastScreenY = e.screenY;
  callApi("move_window", dx, dy);
});
window.addEventListener("mouseup", () => {
  dragging = false;
});

// ── Go fully opaque while focused/hovered, see-through otherwise ───────────
// The real window transparency is OS-level (see app/overlay.py), so the
// page just tells Python when to switch it — CSS opacity can't do this.

window.addEventListener("blur", () => callApi("set_focused", false));
window.addEventListener("focus", () => callApi("set_focused", true));
document.body.addEventListener("mouseenter", () => callApi("set_focused", true));
document.body.addEventListener("mouseleave", () => {
  if (document.hasFocus()) return;
  callApi("set_focused", false);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") callApi("close_overlay");
});

// ── Particle field — ambient drift at idle, swirling gather while thinking,
//    agitated red pulse while listening. Pure canvas, no deps. ─────────────

const particles = (() => {
  const canvas = document.getElementById("particles");
  const ctx = canvas.getContext("2d");
  const COUNT = 60;
  const COLORS = {
    idle: "0, 188, 212",
    thinking: "0, 229, 255",
    listening: "207, 102, 121",
    done: "76, 175, 121",
  };
  // Target overall pace per mode. Idle stays gently in motion (never 0) so the
  // field keeps drifting while you type or read; thinking is fast and busy.
  const TARGET_SPEED = {
    idle: 0.8,
    thinking: 2.6,
    listening: 1.9,
    done: 0.8,
  };
  const BASE_DRIFT = 0.28; // min per-particle velocity → idle never freezes

  let mode = "idle";
  let points = [];
  // Eased global speed so mode changes glide instead of snapping — when JARVIS
  // finishes a reply the field smoothly winds down from its energetic
  // "thinking" pace to a slow idle drift rather than stopping dead.
  let speed = TARGET_SPEED.idle;

  function resize() {
    const app = document.getElementById("app");
    canvas.width = app.clientWidth;
    canvas.height = app.clientHeight;
  }

  function spawn() {
    return {
      x: Math.random() * canvas.width,
      y: Math.random() * canvas.height,
      vx: (Math.random() - 0.5) * 0.25,
      vy: (Math.random() - 0.5) * 0.25,
      r: Math.random() * 1.4 + 0.4,
      a: Math.random() * 0.45 + 0.12,
    };
  }

  function init() {
    resize();
    points = Array.from({ length: COUNT }, spawn);
    requestAnimationFrame(tick);
  }

  function tick() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const cx = canvas.width / 2;
    const cy = canvas.height / 2;
    const color = COLORS[mode] || COLORS.idle;

    // Ease the global pace toward this mode's target so transitions are smooth.
    const target = TARGET_SPEED[mode] ?? TARGET_SPEED.idle;
    speed += (target - speed) * 0.05;

    for (const p of points) {
      if (mode === "thinking") {
        const dx = cx - p.x;
        const dy = cy - p.y;
        const dist = Math.hypot(dx, dy) || 1;
        p.vx += (dx / dist) * 0.018 - (dy / dist) * 0.045;
        p.vy += (dy / dist) * 0.018 + (dx / dist) * 0.045;
      } else if (mode === "listening") {
        p.vx += (Math.random() - 0.5) * 0.04;
        p.vy += (Math.random() - 0.5) * 0.04;
      } else {
        // Idle: a faint random wander keeps the drift alive and organic.
        p.vx += (Math.random() - 0.5) * 0.012;
        p.vy += (Math.random() - 0.5) * 0.012;
      }

      // Light damping reins in the thinking-swirl buildup, but a velocity floor
      // means particles keep drifting at idle instead of decaying to a halt.
      p.vx *= 0.97;
      p.vy *= 0.97;
      const mag = Math.hypot(p.vx, p.vy) || 0.0001;
      if (mag < BASE_DRIFT) {
        p.vx = (p.vx / mag) * BASE_DRIFT;
        p.vy = (p.vy / mag) * BASE_DRIFT;
      }

      p.x += p.vx * speed;
      p.y += p.vy * speed;

      if (p.x < 0) p.x = canvas.width;
      if (p.x > canvas.width) p.x = 0;
      if (p.y < 0) p.y = canvas.height;
      if (p.y > canvas.height) p.y = 0;

      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${color}, ${p.a})`;
      ctx.fill();
    }
    requestAnimationFrame(tick);
  }

  window.addEventListener("resize", resize);

  return {
    init,
    setMode: (m) => {
      mode = m === "done" ? "idle" : m;
    },
  };
})();

particles.init();
