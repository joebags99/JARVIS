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
  scrollToBottom();
}

// Reveal the finished reply. Nothing is shown while JARVIS works (the thinking
// dots stay up the whole time); when the full text arrives we render it, then
// stagger each top-level block so the answer fades in line by line.
function finishAssistantMessage(fullText) {
  if (!streamEl) return;
  const el = streamEl;
  streamEl = null;

  el.classList.remove("thinking");
  el.innerHTML = renderMarkdown(fullText);

  const blocks = Array.from(el.children);
  blocks.forEach((block, i) => {
    block.style.animationDelay = i * 60 + "ms";
    block.classList.add("reveal");
  });

  // Keep the view pinned to the newest content as the lines settle in.
  scrollToBottom();
  setTimeout(scrollToBottom, 140);
  setTimeout(scrollToBottom, 400);
}

function clearTranscript() {
  transcript.querySelectorAll(".msg").forEach((el) => el.remove());
  document.body.classList.remove("has-messages");
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

  let mode = "idle";
  let points = [];

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
    const speedMul = mode === "thinking" ? 2.5 : mode === "listening" ? 1.8 : 1;

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
      }

      p.x += p.vx * speedMul;
      p.y += p.vy * speedMul;
      p.vx *= 0.97;
      p.vy *= 0.97;

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
