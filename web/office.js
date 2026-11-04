// ============ AI Office — client ============
"use strict";

const W = 960, H = 600;
const canvas = document.getElementById("office");
const ctx = canvas.getContext("2d");
const dpr = Math.min(window.devicePixelRatio || 1, 2);
canvas.width = W * dpr; canvas.height = H * dpr; ctx.scale(dpr, dpr);

let TOKEN = localStorage.getItem("officeToken") || "";
let STATE = null;
let selected = null;
const sprites = {};     // id -> sprite

// ---------- helpers ----------
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
const nameOf = (id) => STATE?.employees.find((e) => e.id === id)?.name || id;
const colorOf = (id) => STATE?.employees.find((e) => e.id === id)?.color || "#888";

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-Admin-Token": TOKEN, ...(opts.headers || {}) } });
  if (r.status === 401) {
    TOKEN = prompt("Пароль доступа к офису (OFFICE_ADMIN_TOKEN):") || "";
    localStorage.setItem("officeToken", TOKEN);
    return api(path, opts);
  }
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// ---------- office layout ----------
const DESKS = [
  { x: 130, y: 215 }, { x: 330, y: 215 }, { x: 530, y: 215 },
  { x: 130, y: 405 }, { x: 330, y: 405 }, { x: 530, y: 405 },
];
const DIRECTOR_DESK = { x: 815, y: 200 };
const DIRECTOR_DOOR = { x: 700, y: 300 };
const LOUNGE = [{ x: 735, y: 470 }, { x: 790, y: 520 }, { x: 860, y: 470 }, { x: 900, y: 530 }, { x: 700, y: 540 }, { x: 830, y: 560 }];
const SKIN = ["#f1c7a5", "#e0ac83", "#c68b5e", "#f5d3b8", "#a86f48", "#ebbd95"];
const HAIR = ["#2b1d14", "#7a4b23", "#d9a441", "#111", "#5b2d1a", "#3a3a3a"];

function seatOf(emp, idx) {
  if (emp.role === "director") return { x: DIRECTOR_DESK.x, y: DIRECTOR_DESK.y - 22 };
  const d = DESKS[idx % DESKS.length];
  return { x: d.x, y: d.y - 22 };
}

function ensureSprites() {
  let w = 0;
  STATE.employees.forEach((e, i) => {
    const deskIdx = e.role === "director" ? -1 : w++;
    if (!sprites[e.id]) {
      const seat = seatOf(e, deskIdx);
      sprites[e.id] = { id: e.id, x: seat.x, y: seat.y, path: [], deskIdx, skin: SKIN[i % SKIN.length], hair: HAIR[(i * 2) % HAIR.length],
        bubble: "", bubbleAt: 0, mode: "", walkPhase: 0, waitUntil: 0, lounge: LOUNGE[i % LOUNGE.length] };
    }
    sprites[e.id].emp = e;
  });
}

function routeTo(sp, target) {
  const last = sp.path.length ? sp.path[sp.path.length - 1] : sp;
  if (Math.hypot(last.x - target.x, last.y - target.y) < 2) return;
  const corridorY = 296 + (sp.deskIdx + 1) * 6;
  sp.path = [{ x: sp.x, y: corridorY }, { x: target.x, y: corridorY }, { x: target.x, y: target.y }];
}

function updateTargets() {
  for (const sp of Object.values(sprites)) {
    const e = sp.emp, seat = seatOf(e, sp.deskIdx);
    let mode;
    if (e.role === "director") mode = "seat";
    else if (e.state === "working") mode = "seat";
    else if (e.state === "walking") mode = "deliver";
    else mode = "lounge";
    if (mode === sp.mode) continue;
    sp.mode = mode;
    if (mode === "seat") routeTo(sp, seat);
    else if (mode === "lounge") routeTo(sp, sp.lounge);
    else if (mode === "deliver") { routeTo(sp, DIRECTOR_DOOR); sp.after = "lounge"; }
  }
}

function step(dt) {
  for (const sp of Object.values(sprites)) {
    if (sp.waitUntil > performance.now()) continue;
    if (!sp.path.length) {
      if (sp.after && Math.hypot(sp.x - DIRECTOR_DOOR.x, sp.y - DIRECTOR_DOOR.y) < 3) {
        sp.waitUntil = performance.now() + 1800; // hands over the result
        sp.bubble = "Готово, принимай!"; sp.bubbleAt = performance.now();
        const next = sp.after; sp.after = null;
        setTimeout(() => { if (sp.mode === "deliver") { sp.mode = next; routeTo(sp, sp.lounge); } }, 1800);
      }
      continue;
    }
    const t = sp.path[0], dx = t.x - sp.x, dy = t.y - sp.y, d = Math.hypot(dx, dy), v = 90 * dt;
    if (d <= v) { sp.x = t.x; sp.y = t.y; sp.path.shift(); }
    else { sp.x += (dx / d) * v; sp.y += (dy / d) * v; sp.walkPhase += dt * 10; }
  }
}

// ---------- drawing ----------
function rect(x, y, w, h, c) { ctx.fillStyle = c; ctx.fillRect(Math.round(x), Math.round(y), w, h); }

function drawRoom(now) {
  // floor
  rect(0, 90, W, H - 90, "#3d332a");
  for (let y = 90, r = 0; y < H; y += 24, r++) {
    rect(0, y, W, 1, "#2f271f");
    for (let x = (r % 3) * 40; x < W; x += 120) rect(x, y, 1, 24, "#2f271f");
    if (r % 2) rect(0, y + 1, W, 23, "#40362c");
  }
  // wall
  rect(0, 0, W, 90, "#4a5568"); rect(0, 84, W, 6, "#2d3748");
  // windows with real-time sky
  const hr = new Date().getHours() + new Date().getMinutes() / 60;
  const sky = hr < 6 || hr > 21 ? "#14203b" : hr < 8 || hr > 19 ? "#e58a5a" : "#7ec4f2";
  for (const wx of [40, 200]) {
    rect(wx, 14, 120, 56, "#2d3748"); rect(wx + 4, 18, 112, 48, sky);
    rect(wx + 58, 18, 4, 48, "#2d3748");
    if (hr < 6 || hr > 21) for (let i = 0; i < 6; i++) rect(wx + 10 + i * 17, 24 + (i * 13) % 30, 2, 2, "#fff");
    else { rect(wx + 20, 30, 26, 8, "#ffffffcc"); rect(wx + 70, 40, 30, 8, "#ffffffaa"); }
  }
  // whiteboard with live KPIs
  rect(370, 12, 230, 64, "#cbd5e0"); rect(374, 16, 222, 56, "#f7fafc");
  ctx.fillStyle = "#2b6cb0"; ctx.font = "bold 12px monospace";
  const t = STATE?.tasks || [];
  const cnt = (s) => t.filter((x) => x.status === s).length;
  ctx.fillText(`ЗАДАЧИ  в работе:${cnt("in_progress")}  проверка:${cnt("review")}`, 382, 34);
  ctx.fillStyle = "#2f855a";
  ctx.fillText(`ГОТОВО: ${cnt("done")}   ПРОДАЖИ: ${STATE?.sales.count || 0}`, 382, 52);
  ctx.fillStyle = "#c53030";
  ctx.fillText(`ВЫРУЧКА: ${(STATE?.sales.total || 0).toFixed(0)}`, 382, 68);
  // clock
  const c = { x: 640, y: 44 };
  ctx.fillStyle = "#f7fafc"; ctx.beginPath(); ctx.arc(c.x, c.y, 20, 0, 7); ctx.fill();
  ctx.strokeStyle = "#2d3748"; ctx.lineWidth = 3; ctx.stroke();
  const d = new Date(), hA = ((d.getHours() % 12) + d.getMinutes() / 60) / 12 * 6.283, mA = d.getMinutes() / 60 * 6.283;
  ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(c.x, c.y); ctx.lineTo(c.x + Math.sin(hA) * 10, c.y - Math.cos(hA) * 10); ctx.stroke();
  ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(c.x, c.y); ctx.lineTo(c.x + Math.sin(mA) * 15, c.y - Math.cos(mA) * 15); ctx.stroke();

  // director glass office
  rect(680, 100, 265, 200, "#2a3446");
  ctx.fillStyle = "#9ad0ff22"; ctx.fillRect(680, 100, 265, 200);
  ctx.strokeStyle = "#9ad0ff88"; ctx.lineWidth = 3; ctx.strokeRect(681, 101, 263, 198);
  rect(680, 265, 4, 35, "#40352b"); // door gap
  ctx.fillStyle = "#9ad0ffaa"; ctx.font = "bold 11px system-ui"; ctx.fillText("ГЛАВНЫЙ АССИСТЕНТ", 760, 118);
  // rug
  rect(740, 230, 150, 50, "#7b3f3f"); rect(746, 236, 138, 38, "#934b4b");

  // lounge
  rect(680, 420, 265, 170, "#33403a");
  ctx.fillStyle = "#cfe8d8aa"; ctx.font = "bold 11px system-ui"; ctx.fillText("КУХНЯ / ОТДЫХ", 760, 438);
  rect(700, 448, 120, 28, "#6b4f8a"); rect(700, 440, 120, 12, "#5a4275"); // sofa
  rect(890, 445, 36, 54, "#cbd5e0"); rect(896, 452, 24, 14, "#2d3748"); // coffee machine
  if (Math.floor(now / 500) % 2) rect(906, 470, 4, 4, "#f56565");
  rect(840, 500, 40, 24, "#8b6b4a"); // table
  plant(660, 560); plant(40, 560); plant(940, 330);
}

function plant(x, y) {
  rect(x - 9, y - 4, 18, 16, "#a0522d");
  ctx.fillStyle = "#3f9b52"; ctx.beginPath(); ctx.arc(x, y - 12, 13, 0, 7); ctx.fill();
  ctx.fillStyle = "#57b86a"; ctx.beginPath(); ctx.arc(x - 5, y - 16, 7, 0, 7); ctx.fill();
}

function drawDesk(d, emp, now) {
  rect(d.x - 52, d.y, 104, 34, "#8b6b4a"); rect(d.x - 52, d.y + 30, 104, 6, "#6b4f35");
  rect(d.x - 48, d.y + 36, 6, 14, "#5a4330"); rect(d.x + 42, d.y + 36, 6, 14, "#5a4330");
  // monitor (back side) + glow when working
  const working = emp && (emp.busy || emp.state === "working" || emp.state === "reviewing");
  if (working) { ctx.fillStyle = `rgba(110,168,255,${0.18 + 0.08 * Math.sin(now / 300)})`; ctx.beginPath(); ctx.arc(d.x - 14, d.y - 4, 30, 0, 7); ctx.fill(); }
  rect(d.x - 34, d.y - 18, 40, 26, "#1a202c"); rect(d.x - 16, d.y + 6, 4, 6, "#1a202c");
  rect(d.x - 31, d.y - 15, 4, 3, working ? "#4fd18b" : "#555");
  // papers / mug
  rect(d.x + 14, d.y + 8, 20, 14, "#f7fafc"); rect(d.x + 16, d.y + 11, 14, 1, "#a0aec0"); rect(d.x + 16, d.y + 15, 10, 1, "#a0aec0");
  rect(d.x + 38, d.y + 4, 8, 10, emp?.color || "#ccc");
  if (emp && emp.role !== "director") { ctx.fillStyle = "#e2e8f0cc"; ctx.font = "10px system-ui"; ctx.textAlign = "center"; ctx.fillText(emp.title, d.x, d.y + 62); ctx.textAlign = "left"; }
}

function drawPerson(sp, now) {
  const e = sp.emp, seated = !sp.path.length && sp.mode === "seat";
  const x = Math.round(sp.x), y = Math.round(sp.y);
  const bob = sp.path.length ? Math.sin(sp.walkPhase) * 2 : 0;
  // shadow
  ctx.fillStyle = "#0005"; ctx.beginPath(); ctx.ellipse(x, y + 26, 13, 4, 0, 0, 7); ctx.fill();
  if (!seated) { // legs
    const s = sp.path.length ? Math.sin(sp.walkPhase) * 4 : 0;
    rect(x - 6, y + 12, 5, 14 + s, "#2d3748"); rect(x + 1, y + 12, 5, 14 - s, "#2d3748");
  }
  // body
  rect(x - 10, y - 4 + bob, 20, 18, e.color);
  // arms (typing)
  const typing = seated && (e.busy || e.state === "working" || e.state === "reviewing");
  const a = typing ? (Math.floor(now / 150) % 2) * 3 : 0;
  rect(x - 14, y - 2 + bob + a, 4, 12, e.color); rect(x + 10, y - 2 + bob + (typing ? 3 - a : 0), 4, 12, e.color);
  // head
  rect(x - 8, y - 20 + bob, 16, 16, sp.skin);
  rect(x - 8, y - 22 + bob, 16, 6, sp.hair); rect(x - 9, y - 20 + bob, 3, 8, sp.hair);
  rect(x - 4, y - 13 + bob, 2, 2, "#1a202c"); rect(x + 3, y - 13 + bob, 2, 2, "#1a202c");
  if (e.role === "director") rect(x - 6, y - 26 + bob, 12, 4, "#f6c343"); // crown-ish badge
  if (sp.mode === "lounge" && !sp.path.length) rect(x + 12, y + 2, 6, 7, "#f7fafc"); // coffee cup
  // name
  ctx.font = "bold 11px system-ui"; ctx.textAlign = "center";
  ctx.fillStyle = selected === e.id ? "#fff" : "#e2e8f0"; ctx.fillText(e.name, x, y + 40);
  if (selected === e.id) { ctx.strokeStyle = "#6ea8ff"; ctx.lineWidth = 2; ctx.strokeRect(x - 18, y - 30, 36, 60); }
  ctx.textAlign = "left";
}

function drawBubble(sp, now) {
  const e = sp.emp;
  const text = sp.bubble || e.bubble;
  if (!text) return;
  const age = now - (sp.bubbleAt || 0);
  if (sp.path.length && sp.mode !== "deliver") return;   // no chatter while walking
  const busy = e.busy || sp.mode === "deliver";
  if (!busy && age > 9000) return;
  ctx.font = "11px system-ui";
  const words = text.replace(/\s+/g, " ").slice(0, 90).split(" ");
  const lines = []; let line = "";
  for (const w of words) { if (ctx.measureText(line + " " + w).width > 150) { lines.push(line); line = w; } else line = (line ? line + " " : "") + w; }
  if (line) lines.push(line);
  const shown = lines.slice(0, 3); if (lines.length > 3) shown[2] += "…";
  const bw = Math.max(...shown.map((l) => ctx.measureText(l).width)) + 14, bh = shown.length * 14 + 8;
  let bx = Math.round(sp.x - bw / 2), by = Math.round(sp.y - 34 - bh);
  bx = Math.max(4, Math.min(W - bw - 4, bx)); by = Math.max(4, by);
  ctx.fillStyle = "#fffffff0"; ctx.beginPath(); ctx.roundRect(bx, by, bw, bh, 7); ctx.fill();
  ctx.beginPath(); ctx.moveTo(sp.x - 5, by + bh); ctx.lineTo(sp.x, by + bh + 7); ctx.lineTo(sp.x + 5, by + bh); ctx.fill();
  ctx.fillStyle = "#1a202c"; shown.forEach((l, i) => ctx.fillText(l, bx + 7, by + 16 + i * 14));
}

let last = performance.now();
function frame(now) {
  const dt = Math.min(0.05, (now - last) / 1000); last = now;
  ctx.clearRect(0, 0, W, H);
  drawRoom(now);
  if (STATE) {
    step(dt);
    const deskOwners = {};
    for (const sp of Object.values(sprites)) if (sp.deskIdx >= 0) deskOwners[sp.deskIdx] = sp.emp;
    const items = [];
    DESKS.forEach((d, i) => { if (deskOwners[i]) items.push({ y: d.y + 30, draw: () => drawDesk(d, deskOwners[i], now) }); });
    const dir = Object.values(sprites).find((s) => s.emp.role === "director");
    items.push({ y: DIRECTOR_DESK.y + 30, draw: () => drawDesk(DIRECTOR_DESK, dir?.emp, now) });
    for (const sp of Object.values(sprites)) items.push({ y: sp.y + 26, draw: () => drawPerson(sp, now) });
    items.sort((a, b) => a.y - b.y).forEach((i) => i.draw());
    for (const sp of Object.values(sprites)) drawBubble(sp, now);
    if (STATE.paused || STATE.stopped) {
      ctx.fillStyle = "#000a"; ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = STATE.stopped ? "#ff6b6b" : "#f2b84b"; ctx.font = "bold 32px system-ui"; ctx.textAlign = "center";
      ctx.fillText(STATE.stopped ? "⛔ АВАРИЙНАЯ ОСТАНОВКА" : "⏸ ОФИС НА ПАУЗЕ", W / 2, H / 2); ctx.textAlign = "left";
    }
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

canvas.addEventListener("click", (ev) => {
  const r = canvas.getBoundingClientRect();
  const scale = Math.min(r.width / W, r.height / H);
  const ox = (r.width - W * scale) / 2, oy = (r.height - H * scale) / 2;
  const x = (ev.clientX - r.left - ox) / scale, y = (ev.clientY - r.top - oy) / scale;
  let best = null, bd = 40;
  for (const sp of Object.values(sprites)) { const d = Math.hypot(sp.x - x, sp.y - y); if (d < bd) { bd = d; best = sp.id; } }
  selected = best === selected ? null : best;
  showTab("tasks"); renderPanel();
});

// ---------- panel ----------
const COLS = [["in_progress", "В работе"], ["review", "На проверке"], ["rework", "Доработка"], ["todo", "Очередь"], ["blocked", "Блокеры"], ["done", "Готово"]];
const ST_RU = { todo: "очередь", in_progress: "в работе", review: "проверка", rework: "доработка", done: "готово", blocked: "блокер", cancelled: "отменена",
  pending_review: "на проверке", pending_owner: "ждёт вас", approved: "одобрено", sent: "отправлено", dry_run: "dry-run", rejected: "отклонено", failed: "ошибка" };

function taskCard(t) {
  return `<div class="card"><div class="meta"><span>#${t.id}</span><span class="st ${t.status}">${ST_RU[t.status] || t.status}</span>
    <span class="who"><i class="dot" style="background:${esc(colorOf(t.assignee))}"></i>${esc(nameOf(t.assignee))}</span><span>P${t.priority}</span>
    ${t.attempts ? `<span>доработок: ${t.attempts}</span>` : ""}</div>
    <details><summary>${esc(t.title)}</summary><div class="body">${esc(t.description)}</div>
    ${t.result ? `<div class="body"><b>Результат:</b> ${esc(t.result)}</div>` : ""}
    ${t.files?.length ? `<div class="meta">Файлы: ${t.files.map(esc).join(", ")}</div>` : ""}
    ${t.review_notes ? `<div class="body"><b>Проверка:</b> ${esc(t.review_notes)}</div>` : ""}</details></div>`;
}

function renderPanel() {
  if (!STATE) return;
  const s = STATE;
  $("company").textContent = s.company;
  $("demo").hidden = !s.demo;
  const st = $("status");
  st.textContent = s.stopped ? "остановлен" : s.paused ? "пауза" : "работает 24/7";
  st.className = "pill" + (s.stopped ? " bad" : s.paused ? " warn" : "");
  $("revenue").textContent = s.sales.total.toFixed(2);
  $("salesCount").textContent = s.sales.count;
  $("spend").textContent = `$${s.spend_today.toFixed(2)} / ${s.budget}`;
  $("spendBar").style.width = Math.min(100, (s.spend_today / s.budget) * 100) + "%";
  $("mode").textContent = s.mode === "autonomous" ? "автономный" : "с вашим одобрением";

  // employee card
  const card = $("employeeCard");
  if (selected) {
    const e = s.employees.find((x) => x.id === selected);
    const evs = s.events.filter((x) => x.agent === selected).slice(-12).reverse();
    card.hidden = false;
    card.innerHTML = `<div class="card"><b style="color:${esc(e.color)}">${esc(e.name)}</b> — ${esc(e.title)}
      <div class="meta">${e.busy ? "работает" : "свободен"}${e.task ? ` над #${e.task}` : ""}</div>
      <div class="body">${evs.map((x) => `<div><time>${fmtTime(x.ts)}</time> ${esc(x.text.slice(0, 220))}</div>`).join("") || "Пока тихо"}</div></div>`;
  } else card.hidden = true;

  const tasks = selected ? s.tasks.filter((t) => t.assignee === selected) : s.tasks;
  $("kanban").innerHTML = COLS.map(([k, label]) => {
    const list = tasks.filter((t) => t.status === k);
    const shown = k === "done" ? list.slice(-15).reverse() : list;
    return list.length ? `<div class="col"><h4>${label}<span>${list.length}</span></h4>${shown.map(taskCard).join("")}</div>` : "";
  }).join("") || '<p class="muted">Задач пока нет</p>';

  // questions
  const open = s.escalations.filter((q) => q.status === "open" && q.agent !== "owner");
  $("qBadge").hidden = !open.length; $("qBadge").textContent = open.length;
  if (!document.activeElement?.classList.contains("ans")) {
    $("questions").innerHTML = s.escalations.filter((q) => q.agent !== "owner").map((q) => `<div class="card">
      <div class="meta"><span>${esc(nameOf(q.agent))}</span><span>${fmtTime(q.created_at)}</span><span class="st ${q.status === "open" ? "review" : "done"}">${q.status === "open" ? "ждёт ответа" : "отвечено"}</span></div>
      <div class="body"><b>${esc(q.question)}</b>${q.context ? "\n" + esc(q.context) : ""}</div>
      ${q.status === "open" ? `<textarea class="ans" id="ans-${q.id}" placeholder="Ваш ответ"></textarea><button data-answer="${q.id}">Ответить</button>`
        : `<div class="body">Ответ: ${esc(q.answer)}</div>`}</div>`).join("") || '<p class="muted">Вопросов нет — агенты справляются сами.</p>';
  }

  // comms
  $("outbox").innerHTML = s.outbox.map((m) => `<div class="card"><div class="meta"><span>#${m.id}</span><span class="st ${m.status}">${ST_RU[m.status] || m.status}</span>
    <span>${esc(m.channel)} → ${esc(m.to_addr)}</span><span>${esc(nameOf(m.author))}</span>${m.is_cold ? "<span>холодное</span>" : ""}</div>
    <details><summary>${esc(m.subject || m.body.slice(0, 60))}</summary><div class="body">${esc(m.body)}</div>
    ${m.review_note ? `<div class="meta">Проверка: ${esc(m.review_note)}</div>` : ""}</details>
    ${m.status === "pending_owner" ? `<div class="row"><button class="ok" data-msg="${m.id}" data-ok="1">Отправить</button><button class="danger" data-msg="${m.id}" data-ok="0">Отклонить</button></div>` : ""}</div>`).join("") || '<p class="muted">Пусто</p>';
  $("inbox").innerHTML = s.inbox.map((m) => `<div class="card"><div class="meta"><span>${esc(m.channel)}</span><span>${esc(m.from_name || m.from_addr)}</span><span>${fmtTime(m.received_at)}</span></div>
    <details><summary>${esc(m.subject || m.body.slice(0, 60))}</summary><div class="body">${esc(m.body)}</div></details></div>`).join("") || '<p class="muted">Пусто</p>';

  // sales
  $("products").innerHTML = s.products.map((p) => `<div class="card"><div class="meta"><span>#${p.id}</span><span class="st ${p.status === "on_sale" ? "done" : "review"}">${p.status === "on_sale" ? "в продаже" : esc(p.status)}</span><span>${p.price} ${esc(p.currency.toUpperCase())}</span></div>
    <b>${esc(p.name)}</b><div class="body">${esc(p.description)}</div>${p.payment_url ? `<div class="meta">${/^https:\/\//.test(p.payment_url) ? `<a href="${esc(p.payment_url)}" target="_blank" rel="noopener">ссылка на оплату</a>` : esc(p.payment_url)}</div>` : ""}</div>`).join("") || '<p class="muted">Продуктов пока нет</p>';
  $("salesList").innerHTML = s.sales.recent.map((x) => `<div class="card"><b>${x.amount.toFixed(2)} ${esc(x.currency.toUpperCase())}</b> <span class="muted">${esc(x.customer)} · ${fmtTime(x.ts)} · ${esc(x.source)}</span></div>`).join("") || '<p class="muted">Продаж пока нет</p>';
  $("contacts").innerHTML = s.contacts.map((c) => `<div class="card"><div class="meta"><span>${esc(c.addr)}</span><span class="st">${esc(c.stage)}</span>${c.opted_out ? '<span class="st rejected">отписан</span>' : ""}</div>${esc(c.name)} ${esc(c.company)} <span class="muted">${esc(c.source)}</span></div>`).join("") || '<p class="muted">Контактов пока нет</p>';

  // feed
  renderFeed();

  // connectors
  const C = s.connectors, onoff = (v) => v ? '<span class="st done">подключено</span>' : '<span class="st">dry-run</span>';
  $("connectors").innerHTML = `<div class="card">Почта (SMTP/IMAP) ${onoff(C.email)}</div><div class="card">Telegram ${onoff(C.telegram)}</div>
    <div class="card">Оплата Stripe ${onoff(C.stripe)}</div><div class="card">ИИ (Claude) ${s.demo ? '<span class="st review">демо без ключа</span>' : '<span class="st done">подключено</span>'}</div>`;
}

function renderFeed() {
  $("feed").innerHTML = STATE.events.slice().reverse().map((e) =>
    `<div class="ev ${e.kind === "error" ? "error" : e.kind === "sale" ? "sale" : ""}"><time>${fmtTime(e.ts)}</time><b style="color:${esc(colorOf(e.agent))}">${esc(nameOf(e.agent))}</b> ${esc(e.text.slice(0, 300))}</div>`).join("");
}

function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + name));
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

document.addEventListener("click", async (ev) => {
  const t = ev.target;
  if (t.dataset.answer) {
    const v = $("ans-" + t.dataset.answer).value.trim(); if (!v) return;
    await api(`/api/escalations/${t.dataset.answer}/answer`, { method: "POST", body: JSON.stringify({ answer: v }) }); refresh();
  }
  if (t.dataset.msg) { await api(`/api/outbox/${t.dataset.msg}/decision`, { method: "POST", body: JSON.stringify({ approve: t.dataset.ok === "1" }) }); refresh(); }
});
$("btnPause").onclick = () => api("/api/pause", { method: "POST" }).then(refresh);
$("btnResume").onclick = () => api("/api/resume", { method: "POST" }).then(refresh);
$("btnStop").onclick = () => confirm("Остановить ВСЮ работу и отменить неотправленные сообщения?") && api("/api/stop", { method: "POST" }).then(refresh);
$("btnDirective").onclick = async () => {
  const v = $("directive").value.trim(); if (!v) return;
  await api("/api/tasks", { method: "POST", body: JSON.stringify({ title: v.slice(0, 120), description: v }) });
  $("directive").value = ""; refresh();
};
$("btnInbound").onclick = async () => {
  const from = $("inFrom").value.trim(), body = $("inBody").value.trim(); if (!from || !body) return;
  await api("/api/inbox", { method: "POST", body: JSON.stringify({ channel: from.includes("@") ? "email" : "telegram", from_addr: from, from_name: $("inName").value.trim(), body }) });
  $("inBody").value = ""; refresh();
};
$("btnLogout").onclick = () => { localStorage.removeItem("officeToken"); TOKEN = ""; refresh(); };

// ---------- data ----------
async function refresh() {
  try {
    STATE = await api("/api/state");
    ensureSprites(); updateTargets(); renderPanel();
  } catch (e) { console.warn(e); }
}
refresh();
setInterval(refresh, 3000);

function connectWS() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
  ws.onmessage = (m) => {
    const { event } = JSON.parse(m.data);
    if (!STATE) return;
    STATE.events.push(event); if (STATE.events.length > 120) STATE.events.shift();
    const sp = sprites[event.agent];
    if (sp && ["speech", "tool", "start", "submitted", "approved", "rework", "drafted"].includes(event.kind)) {
      sp.bubble = event.text.replace(/^[a-z_]+: /, ""); sp.bubbleAt = performance.now();
    }
    if (!$("tab-feed").hidden) renderFeed();
  };
  ws.onclose = () => setTimeout(connectWS, 3000);
}
connectWS();
