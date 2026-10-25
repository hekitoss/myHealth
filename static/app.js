// MyHealth — клиентская часть без сборки.
const $ = (sel) => document.querySelector(sel);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const ZONE_VARS = ["--z1", "--z2", "--z3", "--z4", "--z5"];

const state = { workouts: [], days: 30, charts: {}, selected: null, conversationId: newId(), status: {}, chatScope: loadChatScope() };

const SPORT_RU = {
  RUNNING: "Бег", RUNNING_ROAD: "Бег", RUNNING_TRAIL: "Трейл", TREADMILL_RUNNING: "Бег (дорожка)",
  CYCLING: "Велосипед", ROAD_BIKING: "Шоссе", INDOOR_CYCLING: "Велотренажёр", MOUNTAIN_BIKING: "МТБ",
  STRENGTH_TRAINING: "Силовая", WALKING: "Ходьба", HIKING: "Поход", SWIMMING: "Плавание",
  YOGA: "Йога", OTHER: "Другое", BIKING: "Велосипед",
};
const sportName = (s) => SPORT_RU[s] || (s ? s.replaceAll("_", " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()) : "—");

function newId() { return crypto.randomUUID ? crypto.randomUUID() : String(Date.now()); }
function fmtDur(sec) {
  if (!sec) return "—";
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h} ч ${m} мин` : `${m} мин`;
}
function fmtDate(iso, withTime = false) {
  const d = new Date(iso);
  const opts = { day: "numeric", month: "short" };
  if (withTime) Object.assign(opts, { weekday: "short", hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString("ru-RU", opts);
}
const km = (m) => (m ? `${(m / 1000).toFixed(1)} км` : "");

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    // Ошибки проверки полей FastAPI приходят списком.
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail;
    throw new Error(detail || `HTTP ${r.status}`);
  }
  return r.json();
}

function banner(text, isError = false) {
  const b = $("#banner");
  b.textContent = text;
  b.classList.toggle("hidden", !text);
  b.classList.toggle("error", isError);
}

// --- навигация ---------------------------------------------------------------

function show(view) {
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelector(".nav-item.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("hidden", v.id !== `view-${view}`));
  try { localStorage.setItem("view", view); } catch {}
}
document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

// --- загрузка данных ---------------------------------------------------------

async function loadAll() {
  state.status = await api("/api/status");
  state.workouts = await api("/api/workouts");
  renderStatus();
  renderOverview();
  renderWorkoutList();
  renderChatScope();
  renderPlanAiSports();
  renderSettings();
  if (!state.workouts.length) banner("Пока нет тренировок. Подключите Polar, импортируйте файлы или добавьте демо-данные в разделе «Данные и настройки».");
}

function renderStatus() {
  const s = state.status;
  renderUserSwitch();
  const parts = [`${s.workouts} тренировок`];
  if (s.polar_connected) parts.push("Polar подключён");
  if (s.last_sync) parts.push(`синхр. ${fmtDate(s.last_sync, true)}`);
  $("#sync-status").textContent = parts.join(" · ");
}

// --- пользователь ------------------------------------------------------------
// Сервер берёт пользователя из cookie «user», поэтому её достаточно поставить и перезагрузить
// страницу: все данные, диалоги и план загрузятся заново уже для другого человека.

function renderUserSwitch() {
  const { users = [], user } = state.status;
  const box = $("#user-switch");
  box.innerHTML = users.map((u) =>
    `<button type="button" data-user="${u.id}" class="${u.id === user ? "active" : ""}" aria-pressed="${u.id === user}">${u.name}</button>`).join("");
  box.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.user === state.status.user) return;
    document.cookie = `user=${b.dataset.user}; path=/; max-age=${400 * 24 * 3600}; samesite=lax`;
    location.reload();
  }));
  const name = users.find((u) => u.id === user)?.name;
  document.title = name ? `MyHealth · ${name}` : "MyHealth";
}

// --- обзор -------------------------------------------------------------------

function inPeriod(w, days) {
  return new Date(w.start_time) >= new Date(Date.now() - days * 864e5);
}

function renderOverview() {
  const cur = state.workouts.filter((w) => inPeriod(w, state.days));
  const prev = state.workouts.filter((w) => inPeriod(w, state.days * 2) && !inPeriod(w, state.days));
  const sum = (arr, k) => arr.reduce((a, w) => a + (w[k] || 0), 0);
  const avgHr = (arr) => {
    const t = arr.filter((w) => w.avg_hr && w.duration_s);
    const dur = sum(t, "duration_s");
    return dur ? Math.round(t.reduce((a, w) => a + w.avg_hr * w.duration_s, 0) / dur) : null;
  };
  const delta = (a, b, unit = "") => {
    if (a == null || b == null || !b) return "";
    const d = a - b;
    return `${d >= 0 ? "+" : ""}${Math.round(d)}${unit} к прошлому периоду`;
  };
  const z2 = cur.reduce((a, w) => a + ((w.zones || [])[1] || 0), 0);

  const tiles = [
    ["Тренировок", cur.length, delta(cur.length, prev.length)],
    ["Общее время", fmtDur(sum(cur, "duration_s")), delta(sum(cur, "duration_s") / 60, sum(prev, "duration_s") / 60, " мин")],
    ["Средний пульс", avgHr(cur) ? `${avgHr(cur)} <small>уд/мин</small>` : "—", delta(avgHr(cur), avgHr(prev))],
    ["Время в зоне 2", fmtDur(z2), "базовая выносливость"],
  ];
  $("#tiles").innerHTML = tiles.map(([l, v, s]) =>
    `<div class="tile"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${s}</div></div>`).join("");

  renderWeeklyCharts();
  renderHrTrend();
  $("#recent").innerHTML = state.workouts.slice(0, 6).map(rowHtml).join("") || `<p class="empty">Нет данных</p>`;
  bindRows($("#recent"), true);
}

async function renderWeeklyCharts() {
  const weeks = await api("/api/weekly?weeks=12");
  const labels = weeks.map((w) => fmtDate(w.week));
  chart("chart-weekly", {
    type: "bar",
    data: { labels, datasets: [{ label: "Минуты", data: weeks.map((w) => w.duration_min), backgroundColor: css("--chart-1"), borderRadius: 4, maxBarThickness: 28 }] },
    options: baseOpts({ legend: false }),
  });
  chart("chart-zones", {
    type: "bar",
    data: {
      labels,
      datasets: ZONE_VARS.map((v, i) => ({
        label: `Z${i + 1}`, data: weeks.map((w) => w.zones_min[i]), backgroundColor: css(v), stack: "z", maxBarThickness: 28,
      })),
    },
    options: baseOpts({ stacked: true }),
  });
}

function renderHrTrend() {
  const pts = [...state.workouts].reverse().filter((w) => w.avg_hr && inPeriod(w, Math.max(state.days, 30)));
  chart("chart-hr-trend", {
    type: "line",
    data: {
      labels: pts.map((w) => fmtDate(w.start_time)),
      datasets: [{
        label: "Средний пульс", data: pts.map((w) => w.avg_hr),
        borderColor: css("--chart-1"), backgroundColor: css("--chart-1"), borderWidth: 2, pointRadius: 3, pointHoverRadius: 5, tension: 0.25,
      }],
    },
    options: baseOpts({
      legend: false,
      tooltip: { callbacks: { afterLabel: (c) => sportName(pts[c.dataIndex].sport) } },
    }),
  });
}

// Графики берут цвета из токенов, поэтому работают в обеих темах.
function tooltipStyle() {
  return {
    backgroundColor: css("--surface"), borderColor: css("--border"), borderWidth: 1,
    titleColor: css("--text"), bodyColor: css("--text-muted"), footerColor: css("--text-muted"),
    titleFont: { size: 13, weight: "600" }, bodyFont: { size: 13 },
    padding: 10, cornerRadius: 10, boxPadding: 4, usePointStyle: true,
  };
}

function baseOpts({ legend = true, stacked = false, tooltip = {} } = {}) {
  const grid = css("--border"), text = css("--text-muted");
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: legend, position: "bottom", labels: { color: text, usePointStyle: true, pointStyle: "circle", boxWidth: 8, boxHeight: 8, padding: 14, font: { size: 13 } } },
      tooltip: { ...tooltipStyle(), ...tooltip },
    },
    scales: {
      x: { stacked, border: { display: false }, grid: { display: false }, ticks: { color: text, font: { size: 12 }, maxRotation: 0, autoSkip: true } },
      y: { stacked, border: { display: false }, grid: { color: grid }, ticks: { color: text, font: { size: 12 } } },
    },
  };
}

Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

function chart(id, config) {
  state.charts[id]?.destroy();
  state.charts[id] = new Chart(document.getElementById(id), config);
}

document.querySelectorAll("#period button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#period button").forEach((x) => x.classList.toggle("active", x === b));
  state.days = Number(b.dataset.days);
  renderOverview();
}));

// --- список и детали тренировки ---------------------------------------------

const HEART_ICON = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 13.5S2 10 2 5.8A3 3 0 0 1 8 4.4a3 3 0 0 1 6 1.4C14 10 8 13.5 8 13.5z"/></svg>`;

function rowHtml(w) {
  return `<div class="wrow ${state.selected === w.id ? "active" : ""}" data-id="${w.id}">
    <div class="date">${fmtDate(w.start_time, true)}</div>
    <div class="sport">${sportName(w.sport)}</div>
    <div class="meta">${fmtDur(w.duration_s)}${w.distance_m ? `<br><span class="dist">${km(w.distance_m)}</span>` : ""}</div>
    <div class="hr-pill">${w.avg_hr ? HEART_ICON + w.avg_hr : ""}</div>
  </div>`;
}

function bindRows(container, jump) {
  container.querySelectorAll(".wrow").forEach((r) => r.addEventListener("click", () => {
    if (jump) show("workouts");
    openWorkout(Number(r.dataset.id));
  }));
}

function renderWorkoutList() {
  const sel = $("#sport-filter");
  const sports = [...new Set(state.workouts.map((w) => w.sport).filter(Boolean))];
  const current = sel.value;
  sel.innerHTML = `<option value="">Все виды</option>` + sports.map((s) => `<option value="${s}">${sportName(s)}</option>`).join("");
  sel.value = sports.includes(current) ? current : "";
  const list = state.workouts.filter((w) => !sel.value || w.sport === sel.value);
  $("#workout-list").innerHTML = list.map(rowHtml).join("") || `<p class="empty">Нет тренировок</p>`;
  bindRows($("#workout-list"), false);
}
$("#sport-filter").addEventListener("change", renderWorkoutList);

// Границы зон пульса (50/60/70/80/90% HRmax, как в metrics.ZONE_BOUNDS) — пунктир с подписью.
function zoneBounds() {
  const max = state.status.hr_max;
  return max ? ZONE_VARS.map((v, i) => ({ zone: i + 1, hr: Math.round(max * (0.5 + i / 10)), color: css(v) })) : [];
}

const zoneLinesPlugin = {
  id: "zoneLines",
  afterDatasetsDraw(chart, _args, opts) {
    const { ctx, chartArea: area, scales: { y } } = chart;
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.font = `600 11px ${Chart.defaults.font.family}`;
    ctx.textAlign = "right";
    ctx.textBaseline = "bottom";
    for (const b of opts.bounds || []) {
      const py = y.getPixelForValue(b.hr);
      if (py < area.top || py > area.bottom) continue;
      ctx.strokeStyle = ctx.fillStyle = b.color;
      ctx.beginPath();
      ctx.moveTo(area.left, py);
      ctx.lineTo(area.right, py);
      ctx.stroke();
      ctx.fillText(`Z${b.zone} · ${b.hr}`, area.right - 4, py - 2);
    }
    ctx.restore();
  },
};

async function openWorkout(id) {
  state.selected = id;
  renderWorkoutList();
  const w = await api(`/api/workouts/${id}`);
  const zones = w.zones || [];
  const zTotal = zones.reduce((a, b) => a + b, 0) || 1;
  $("#workout-detail").innerHTML = `
    <h1>${sportName(w.sport)}</h1>
    <div class="muted">${fmtDate(w.start_time, true)} · ${w.source}</div>
    <div class="detail-stats">
      <div>Длительность<b>${fmtDur(w.duration_s)}</b></div>
      <div>Средний пульс<b>${w.avg_hr ?? "—"}</b></div>
      <div>Макс. пульс<b>${w.max_hr ?? "—"}</b></div>
      <div>${w.distance_m ? "Дистанция" : "Калории"}<b>${w.distance_m ? km(w.distance_m) : (w.calories ?? "—")}</b></div>
    </div>
    <h2>Пульс</h2>
    ${w.hr.length ? `<div class="chart-box"><canvas id="chart-workout"></canvas></div>` : `<p class="empty">Нет посекундного пульса</p>`}
    ${zones.length ? `
      <h2>Зоны</h2>
      <div class="zonebar">${zones.map((z, i) => `<div style="width:${(z / zTotal) * 100}%;background:var(${ZONE_VARS[i]})" title="Z${i + 1}: ${fmtDur(z)}"></div>`).join("")}</div>
      <div class="zone-legend">${zones.map((z, i) => `<span><i style="background:var(${ZONE_VARS[i]})"></i>Z${i + 1} ${fmtDur(z)}</span>`).join("")}</div>` : ""}
    <div class="row" style="margin-top:16px">
      <button class="btn secondary" id="ask-about">Спросить AI об этой тренировке</button>
    </div>`;
  if (w.hr.length) {
    const step = Math.max(1, Math.round(w.hr.length / 600)); // не больше ~600 точек
    const pts = [];
    for (let i = 0; i < w.hr.length; i += step) pts.push({ x: (i * w.hr_rate_s) / 60, y: w.hr[i] });
    const bounds = zoneBounds();
    // Шкала дотягивается до ближайшей границы выше максимума, чтобы была видна зона, в которую не зашли.
    const dataMax = Math.max(...w.hr);
    const nextBound = bounds.find((b) => b.hr > dataMax);
    const opts = baseOpts({ legend: false, tooltip: { callbacks: { title: (c) => `${Math.round(c[0].parsed.x)} мин` } } });
    chart("chart-workout", {
      type: "line",
      data: { datasets: [{ label: "Пульс", data: pts, borderColor: css("--chart-1"), borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, tension: 0.2 }] },
      options: {
        ...opts,
        plugins: { ...opts.plugins, zoneLines: { bounds } },
        parsing: false,
        scales: {
          x: { type: "linear", border: { display: false }, ticks: { color: css("--text-muted"), font: { size: 12 }, callback: (v) => `${Math.round(v)} мин` }, grid: { display: false } },
          y: { suggestedMax: nextBound ? nextBound.hr + 5 : undefined, border: { display: false }, ticks: { color: css("--text-muted"), font: { size: 12 } }, grid: { color: css("--border") } },
        },
      },
      plugins: [zoneLinesPlugin],
    });
  }
  if (matchMedia("(max-width: 980px)").matches) $("#workout-detail").scrollIntoView({ behavior: "smooth", block: "start" });
  $("#ask-about").addEventListener("click", () => {
    if (!inChatScope(w)) { state.chatScope = { sport: sportName(w.sport), days: null }; saveChatScope(); renderChatScope(); }
    show("chat");
    $("#chat-input").value = `Разбери тренировку id=${w.id} (${sportName(w.sport)}, ${fmtDate(w.start_time, true)}): как вёл себя пульс, были ли дрейф и интервалы, что можно улучшить?`;
    autoGrow();
    $("#chat-input").focus();
  });
}

// --- чат ---------------------------------------------------------------------

// Выборка тренировок для AI: вид спорта (по русскому названию — «Бег» объединяет все варианты бега) и период.
function loadChatScope() {
  try { return { sport: "", days: null, ...JSON.parse(localStorage.getItem("chatScope")) }; } catch { return { sport: "", days: null }; }
}
function saveChatScope() {
  try { localStorage.setItem("chatScope", JSON.stringify(state.chatScope)); } catch {}
}
function chatScopeSports() {
  const { sport } = state.chatScope;
  if (!sport) return [];
  return [...new Set(state.workouts.map((w) => w.sport).filter((s) => s && sportName(s) === sport))];
}
function inChatScope(w) {
  const { sport, days } = state.chatScope;
  // Как на сервере: период считается от начала дня N дней назад.
  const since = new Date(); since.setHours(0, 0, 0, 0); since.setDate(since.getDate() - (days || 0));
  return (!sport || sportName(w.sport) === sport) && (!days || new Date(w.start_time) >= since);
}
function renderChatScope() {
  const sel = $("#chat-sport");
  const names = [...new Set(state.workouts.map((w) => w.sport).filter(Boolean).map(sportName))].sort((a, b) => a.localeCompare(b, "ru"));
  if (state.chatScope.sport && !names.includes(state.chatScope.sport)) state.chatScope.sport = "";
  sel.innerHTML = `<option value="">Все виды</option>` + names.map((n) => `<option value="${n}">${n}</option>`).join("");
  sel.value = state.chatScope.sport;
  document.querySelectorAll("#chat-period button").forEach((b) =>
    b.classList.toggle("active", (Number(b.dataset.days) || null) === state.chatScope.days));
  const n = state.workouts.filter(inChatScope).length;
  $("#chat-scope-count").textContent = `AI видит ${n} ${plural(n, "тренировку", "тренировки", "тренировок")}`;
}
function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
}
$("#chat-sport").addEventListener("change", (e) => { state.chatScope.sport = e.target.value; saveChatScope(); renderChatScope(); });
document.querySelectorAll("#chat-period button").forEach((b) => b.addEventListener("click", () => {
  state.chatScope.days = Number(b.dataset.days) || null;
  saveChatScope();
  renderChatScope();
}));

function addMsg(role, text = "") {
  $("#chat-empty")?.remove();
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.textContent = text;
  $("#chat-log").appendChild(el);
  el.scrollIntoView({ block: "end" });
  return el;
}

function renderMarkdown(el, text) {
  el.innerHTML = DOMPurify.sanitize(marked.parse(text));
}

const ERROR_MARK = "\x1e";  // как ai.ERROR_MARK на сервере: дальше в потоке идёт текст ошибки

async function sendChat(text) {
  text = text.trim();
  if (!text) return;
  $("#chat-input").value = "";
  autoGrow();
  $("#chat-send").disabled = true;
  const question = addMsg("user", text);
  const out = addMsg("ai");
  out.classList.add("pending");
  let acc = "", error = null;
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: state.conversationId, message: text, sports: chatScopeSports(), days: state.chatScope.days }),
    });
    if (!r.ok) {
      // Ошибку AI сервер отдаёт её же статусом и текстом с меткой; остальное — FastAPI-ошибка.
      const body = await r.text();
      if (body.startsWith(ERROR_MARK)) throw new Error(body.slice(1).trim());
      let detail;
      try { detail = JSON.parse(body).detail; } catch {}
      throw new Error(typeof detail === "string" ? detail : `Сервер ответил ${r.status}.`);
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      acc += dec.decode(value, { stream: true });
      const mark = acc.indexOf(ERROR_MARK);
      if (mark >= 0) { error = acc.slice(mark + 1).trim() || "Неизвестная ошибка."; break; }
      renderMarkdown(out, acc);
      out.scrollIntoView({ block: "end" });
    }
    if (!error && !acc.trim()) error = "Сервер вернул пустой ответ.";
  } catch (e) {
    error = e instanceof TypeError ? "Нет связи с сервером." : e.message;
  } finally {
    out.classList.remove("pending");
    $("#chat-send").disabled = false;
  }
  if (error) {
    // Частичный ответ не показываем: сервер его не сохранил, повтор задаст вопрос заново.
    out.remove();
    showChatError(question, text, error);
  }
  loadChatSessions();
}

function showChatError(question, text, error) {
  const el = document.createElement("div");
  el.className = "chat-error";
  el.setAttribute("role", "alert");
  el.innerHTML = `<span>Не удалось получить ответ. ${esc(error)}</span><button class="btn secondary">Повторить</button>`;
  el.querySelector("button").addEventListener("click", () => {
    if ($("#chat-send").disabled) return;
    el.remove();
    question.remove();
    sendChat(text);
  });
  question.after(el);
  el.scrollIntoView({ block: "end" });
}

$("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendChat($("#chat-input").value); });
function autoGrow() {
  const t = $("#chat-input");
  t.style.height = "auto";
  t.style.height = `${Math.min(t.scrollHeight + 2, 160)}px`;
}
$("#chat-input").addEventListener("input", autoGrow);
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat($("#chat-input").value); }
});
document.querySelectorAll(".suggestions button").forEach((b) => b.addEventListener("click", () => sendChat(b.textContent)));
function newChat() {
  // Прошлый диалог остаётся в истории — его можно открыть и продолжить.
  state.conversationId = newId();
  $("#chat-log").innerHTML = `<div class="chat-empty" id="chat-empty"><p>Новый диалог. Спросите что-нибудь о своих тренировках.</p></div>`;
  renderChatSessions();
}
$("#chat-reset").addEventListener("click", () => { if (!$("#chat-send").disabled) newChat(); });

// --- история диалогов --------------------------------------------------------

state.chatSessions = [];
const esc = (t) => String(t).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

async function loadChatSessions() {
  try { state.chatSessions = await api("/api/chat/sessions"); } catch { return; }
  renderChatSessions();
}

function renderChatSessions() {
  const box = $("#chat-sessions");
  if (!state.chatSessions.length) {
    box.innerHTML = `<p class="empty">Здесь появятся ваши диалоги с AI.</p>`;
    return;
  }
  box.innerHTML = state.chatSessions.map((s) => `
    <div class="srow${s.id === state.conversationId ? " active" : ""}" data-id="${esc(s.id)}" tabindex="0" role="button">
      <span class="title" title="${esc(s.title)}">${esc(s.title)}</span>
      <span class="sub">${fmtDate(s.updated_at, true)} · ${s.questions} ${plural(s.questions, "вопрос", "вопроса", "вопросов")}</span>
    </div>`).join("");
  box.querySelectorAll(".srow").forEach((row) => {
    row.addEventListener("click", () => openChatSession(row.dataset.id));
    row.addEventListener("keydown", (e) => { if (e.target === row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openChatSession(row.dataset.id); } });
    row.addEventListener("contextmenu", (e) => { e.preventDefault(); openSessionMenu(row, e); });
    // iOS не присылает contextmenu — открываем меню долгим нажатием сами.
    let timer;
    row.addEventListener("touchstart", (e) => {
      const t = e.touches[0];
      timer = setTimeout(() => { row.dataset.longPress = "1"; openSessionMenu(row, { clientX: t.clientX, clientY: t.clientY }); }, 500);
    }, { passive: true });
    for (const ev of ["touchend", "touchmove", "touchcancel"]) row.addEventListener(ev, () => clearTimeout(timer), { passive: true });
    row.addEventListener("click", (e) => { if (row.dataset.longPress) { e.stopImmediatePropagation(); delete row.dataset.longPress; } }, true);
  });
}

// Контекстное меню сессии: правая кнопка мыши, долгое нажатие или клавиша меню / Shift+F10.
const sessionMenu = document.createElement("div");
sessionMenu.className = "ctx-menu";
sessionMenu.setAttribute("role", "menu");
sessionMenu.hidden = true;
sessionMenu.innerHTML = `<button role="menuitem" class="danger" data-action="delete">Удалить диалог</button>`;
document.body.appendChild(sessionMenu);

function openSessionMenu(row, e) {
  sessionMenu.dataset.id = row.dataset.id;
  sessionMenu.hidden = false;
  const r = row.getBoundingClientRect();
  // С клавиатуры координат нет (0, 0) — ставим меню под строкой.
  const x = e.clientX || r.left + 16, y = e.clientY || r.bottom;
  const { width, height } = sessionMenu.getBoundingClientRect();
  sessionMenu.style.left = `${Math.min(x, innerWidth - width - 8)}px`;
  sessionMenu.style.top = `${Math.min(y, innerHeight - height - 8)}px`;
  sessionMenu.querySelector("button").focus();
}
function closeSessionMenu() {
  if (sessionMenu.hidden) return;
  sessionMenu.hidden = true;
  document.querySelector(`.srow[data-id="${CSS.escape(sessionMenu.dataset.id)}"]`)?.focus({ preventScroll: true });
}
sessionMenu.addEventListener("click", (e) => {
  const action = e.target.closest("button")?.dataset.action;
  const id = sessionMenu.dataset.id;
  sessionMenu.hidden = true;
  if (action === "delete") deleteChatSession(id);
});
document.addEventListener("pointerdown", (e) => { if (!sessionMenu.contains(e.target)) closeSessionMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSessionMenu(); });
addEventListener("resize", closeSessionMenu);
document.addEventListener("scroll", closeSessionMenu, true);

async function openChatSession(id) {
  if ($("#chat-send").disabled) return;  // идёт ответ — не переключаемся посреди потока
  let s;
  try { s = await api(`/api/chat/sessions/${encodeURIComponent(id)}`); } catch (e) { banner(`Не удалось открыть диалог: ${e.message}`, true); return; }
  state.conversationId = s.id;
  // Возвращаем выборку, с которой задан последний вопрос, — продолжение пойдёт по тем же данным.
  state.chatScope = { sport: s.sports.length ? sportName(s.sports[0]) : "", days: s.days || null };
  saveChatScope();
  renderChatScope();
  $("#chat-log").innerHTML = "";
  let lastQuestion;
  for (const m of s.messages) {
    if (m.role === "user") lastQuestion = addMsg("user", m.text);
    else renderMarkdown(addMsg("ai"), m.text);
  }
  lastQuestion?.scrollIntoView({ block: "start" });
  renderChatSessions();
  if (matchMedia("(max-width: 980px)").matches) setHistoryVisible(false);
}

async function deleteChatSession(id) {
  try { await api(`/api/chat/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }); } catch (e) { banner(`Не удалось удалить: ${e.message}`, true); return; }
  if (id === state.conversationId && !$("#chat-send").disabled) newChat();
  await loadChatSessions();
}

function setHistoryVisible(on) {
  $("#chat-layout").classList.toggle("no-history", !on);
  $("#chat-history-toggle").setAttribute("aria-expanded", String(on));
  try { localStorage.setItem("chatHistory", on ? "1" : "0"); } catch {}
}
$("#chat-history-toggle").addEventListener("click", () => setHistoryVisible($("#chat-layout").classList.contains("no-history")));
(function initHistory() {
  let saved = null;
  try { saved = localStorage.getItem("chatHistory"); } catch {}
  // На узком экране список по умолчанию свёрнут, чтобы не отнимать место у чата.
  setHistoryVisible(saved ? saved === "1" : !matchMedia("(max-width: 980px)").matches);
})();

// --- план тренировок ---------------------------------------------------------

const PLAN_SPORTS = ["RUNNING", "TREADMILL_RUNNING", "CYCLING", "INDOOR_CYCLING", "STRENGTH_TRAINING", "SWIMMING", "WALKING", "HIKING", "YOGA", "OTHER"];
const JSON_HEADERS = { "Content-Type": "application/json" };
// Даты плана — день без времени, в местном часовом поясе.
const isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const dayDate = (s) => new Date(`${s}T00:00`);
const dayLabel = (s) => dayDate(s).toLocaleDateString("ru-RU", { weekday: "short", day: "numeric", month: "short" });
function weekStart(s) {
  const d = dayDate(s);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return isoDay(d);
}

state.planned = [];
state.proposal = null;
state.editing = null;

function planSports() {
  // Свои виды спорта из истории — после стандартных, если их русское название ещё не встречалось.
  const names = new Set(PLAN_SPORTS.map(sportName));
  const own = [];
  for (const s of state.workouts.map((w) => w.sport)) {
    if (s && !names.has(sportName(s))) { names.add(sportName(s)); own.push(s); }
  }
  return [...PLAN_SPORTS, ...own];
}
function sportOptions(selected) {
  const list = planSports();
  if (selected && !list.includes(selected)) list.push(selected);
  return list.map((s) => `<option value="${esc(s)}"${s === selected ? " selected" : ""}>${esc(sportName(s))}</option>`).join("");
}
function renderPlanAiSports() {
  const sel = $("#plan-sport"), cur = sel.value;
  sel.innerHTML = `<option value="">На выбор AI</option>` + sportOptions();
  sel.value = cur;
}

async function loadPlanned() {
  try { state.planned = await api("/api/planned"); } catch (e) { banner(`Не удалось загрузить план: ${e.message}`, true); return; }
  renderPlan();
}

function targetHtml(p) {
  const parts = [];
  if (p.zone) parts.push(`<span class="ztag"><i style="background:var(--z${p.zone})"></i>Z${p.zone}</span>`);
  if (p.hr_low || p.hr_high) parts.push(HEART_ICON + [p.hr_low, p.hr_high].filter(Boolean).join("–"));
  if (p.pace) parts.push(`${esc(p.pace)}/км`);
  return parts.join(" · ");
}

function planRowHtml(p, proposalIndex) {
  const proposal = proposalIndex !== undefined;
  const meta = [dayLabel(p.date), p.duration_min ? fmtDur(p.duration_min * 60) : "", p.distance_km ? `${p.distance_km} км` : ""].filter(Boolean).join(" · ");
  const check = proposal
    ? `<input type="checkbox" class="pcheck" data-index="${proposalIndex}" checked aria-label="Сохранить в план">`
    : `<input type="checkbox" class="pcheck"${p.done ? " checked" : ""} aria-label="Выполнено" title="Выполнено">`;
  return `<div class="prow${p.done ? " done" : ""}"${proposal ? "" : ` data-id="${p.id}" tabindex="0" role="button"`}>
    ${check}
    <div class="pmain">
      <div class="ptitle">${esc(sportName(p.sport))}${p.title ? ` · ${esc(p.title)}` : ""}</div>
      <div class="psub">${meta}</div>
      ${p.notes ? `<div class="pnotes">${esc(p.notes)}</div>` : ""}
    </div>
    <div class="ptarget">${targetHtml(p)}</div>
  </div>`;
}

// Строки, сгруппированные по неделям (с понедельника), с суммарным временем недели.
function byWeekHtml(items, rowFn) {
  const mins = {};
  for (const p of items) mins[weekStart(p.date)] = (mins[weekStart(p.date)] || 0) + (p.duration_min || 0);
  let html = "", week = null;
  items.forEach((p, i) => {
    const w = weekStart(p.date);
    if (w !== week) {
      week = w;
      const end = dayDate(w); end.setDate(end.getDate() + 6);
      html += `<div class="pweek"><span>${fmtDate(dayDate(w))} – ${fmtDate(end)}</span><span>${mins[w] ? fmtDur(mins[w] * 60) : ""}</span></div>`;
    }
    html += rowFn(p, i);
  });
  return html;
}

// Карточка на «Обзоре»: ближайшая невыполненная тренировка из плана.
function renderNextPlanned() {
  const today = isoDay(new Date());
  const p = state.planned.find((x) => x.date >= today && !x.done);
  const box = $("#next-plan");
  if (!p) {
    box.innerHTML = `<div class="next-head"><h2>Следующая тренировка</h2></div>
      <p class="muted">В плане пока ничего нет.</p>
      <div class="row"><button class="btn secondary" id="next-go">Запланировать</button></div>`;
    $("#next-go").addEventListener("click", () => show("plan"));
    return;
  }
  const tomorrow = new Date(); tomorrow.setDate(tomorrow.getDate() + 1);
  const when = p.date === today ? "Сегодня" : p.date === isoDay(tomorrow) ? "Завтра" : dayLabel(p.date);
  const meta = [p.duration_min ? fmtDur(p.duration_min * 60) : "", p.distance_km ? `${p.distance_km} км` : ""].filter(Boolean).join(" · ");
  const target = targetHtml(p);
  box.innerHTML = `<div class="next-head"><h2>Следующая тренировка</h2><span class="next-when">${when}</span></div>
    <div class="next-title">${esc(sportName(p.sport))}${p.title ? ` · ${esc(p.title)}` : ""}</div>
    ${meta || target ? `<div class="next-meta">${[meta, target].filter(Boolean).join(" · ")}</div>` : ""}
    ${p.notes ? `<p class="next-notes">${esc(p.notes)}</p>` : ""}
    <div class="row">
      <button class="btn secondary" id="next-done">Выполнено</button>
      <button class="btn secondary" id="next-open">Подробнее</button>
      <button class="btn secondary" id="next-all">Весь план</button>
    </div>`;
  $("#next-done").addEventListener("click", (e) => { e.target.disabled = true; savePlanned({ ...p, done: true }, p.id); });
  $("#next-open").addEventListener("click", () => openPlanDialog(p));
  $("#next-all").addEventListener("click", () => show("plan"));
}

function renderPlan() {
  renderNextPlanned();
  const today = isoDay(new Date());
  const items = state.planned.filter((p) => $("#plan-show-past").checked || p.date >= today);
  const box = $("#plan-list");
  box.innerHTML = items.length ? byWeekHtml(items, (p) => planRowHtml(p))
    : `<p class="empty">${state.planned.length ? "Ближайших тренировок в плане нет." : "Добавьте тренировку или попросите AI составить план под цель."}</p>`;
  box.querySelectorAll(".prow").forEach((row) => {
    const p = state.planned.find((x) => x.id === Number(row.dataset.id));
    row.addEventListener("click", (e) => { if (!e.target.closest(".pcheck")) openPlanDialog(p); });
    row.addEventListener("keydown", (e) => { if (e.target === row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openPlanDialog(p); } });
    row.querySelector(".pcheck").addEventListener("change", (e) => savePlanned({ ...p, done: e.target.checked }, p.id));
  });
}
$("#plan-show-past").addEventListener("change", renderPlan);

async function savePlanned(data, id) {
  try {
    await api(id ? `/api/planned/${id}` : "/api/planned", { method: id ? "PUT" : "POST", headers: JSON_HEADERS, body: JSON.stringify(data) });
  } catch (e) { banner(`Не удалось сохранить: ${e.message}`, true); return false; }
  await loadPlanned();
  return true;
}

// --- форма тренировки

const PF = { date: "#pf-date", sport: "#pf-sport", title: "#pf-title", duration_min: "#pf-duration", distance_km: "#pf-distance",
  zone: "#pf-zone", pace: "#pf-pace", hr_low: "#pf-hr-low", hr_high: "#pf-hr-high", notes: "#pf-notes" };

function openPlanDialog(p = null) {
  state.editing = p;
  const tomorrow = new Date(); tomorrow.setDate(tomorrow.getDate() + 1);
  $("#pf-sport").innerHTML = sportOptions(p?.sport || state.planned.at(-1)?.sport || "RUNNING");
  for (const [k, sel] of Object.entries(PF)) if (k !== "sport") $(sel).value = p?.[k] ?? "";
  if (!p) $("#pf-date").value = isoDay(tomorrow);
  $("#pf-done").checked = !!p?.done;
  $("#plan-dialog-title").textContent = p ? "Тренировка в плане" : "Новая тренировка";
  $("#pf-delete").classList.toggle("hidden", !p);
  $("#plan-dialog").showModal();
}
$("#plan-add").addEventListener("click", () => openPlanDialog());
$("#pf-cancel").addEventListener("click", () => $("#plan-dialog").close());

// Выбрали зону — подставляем её пульс по HRmax, если диапазон ещё не задан.
$("#pf-zone").addEventListener("change", (e) => {
  const z = Number(e.target.value), max = state.status.hr_max;
  if (!z || !max || $("#pf-hr-low").value || $("#pf-hr-high").value) return;
  $("#pf-hr-low").value = Math.round(max * (0.4 + z / 10));
  $("#pf-hr-high").value = Math.round(max * (0.5 + z / 10));
});

$("#plan-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = { goal: state.editing?.goal ?? null, done: $("#pf-done").checked };
  for (const [k, sel] of Object.entries(PF)) {
    const v = $(sel).value.trim();
    data[k] = v === "" ? null : $(sel).type === "number" || k === "zone" ? Number(v) : v;
  }
  if (await savePlanned(data, state.editing?.id)) $("#plan-dialog").close();
});

$("#pf-delete").addEventListener("click", async () => {
  const p = state.editing;
  if (!p || !confirm("Удалить тренировку из плана?")) return;
  try { await api(`/api/planned/${p.id}`, { method: "DELETE" }); } catch (e) { banner(`Не удалось удалить: ${e.message}`, true); return; }
  $("#plan-dialog").close();
  await loadPlanned();
});

// --- план от AI

function setPlanAi(on) {
  $("#plan-ai").classList.toggle("hidden", !on);
  $("#plan-ai-toggle").setAttribute("aria-expanded", String(on));
  if (on && !$("#plan-goal").value) $("#plan-goal").focus();
}
$("#plan-ai-toggle").addEventListener("click", () => setPlanAi($("#plan-ai").classList.contains("hidden")));
document.querySelectorAll("#plan-goal-presets button").forEach((b) => b.addEventListener("click", () => {
  $("#plan-goal").value = b.textContent;
  $("#plan-goal").focus();
}));

$("#plan-ai-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const goal = $("#plan-goal").value.trim();
  if (goal.length < 3) { $("#plan-goal").focus(); return; }
  const btn = $("#plan-generate");
  btn.disabled = true;
  btn.textContent = "Составляю…";
  $("#plan-proposal").innerHTML = `<p class="muted proposal-wait">AI изучает историю и составляет план, это может занять до минуты…</p>`;
  try {
    const r = await api("/api/planned/generate", {
      method: "POST", headers: JSON_HEADERS,
      body: JSON.stringify({
        goal, start: $("#plan-start").value, weeks: Number($("#plan-weeks").value),
        per_week: Number($("#plan-per-week").value), sports: $("#plan-sport").value ? [$("#plan-sport").value] : [],
      }),
    });
    state.proposal = { ...r, goal };
    renderProposal();
  } catch (err) {
    $("#plan-proposal").innerHTML = `<div class="error-box proposal-wait">${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = state.proposal ? "Составить заново" : "Составить план";
  }
});

function renderProposal() {
  const pr = state.proposal, box = $("#plan-proposal");
  $("#plan-generate").classList.toggle("secondary", !!pr);
  if (!pr) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="proposal">
    ${pr.summary ? `<div class="proposal-summary">${DOMPurify.sanitize(marked.parse(pr.summary))}</div>` : ""}
    <div class="proposal-list">${byWeekHtml(pr.workouts, (p, i) => planRowHtml(p, i))}</div>
    <div class="row">
      <button class="btn" id="proposal-save"></button>
      <button class="btn secondary" id="proposal-cancel">Отменить</button>
    </div>
  </div>`;
  const checks = [...box.querySelectorAll(".pcheck")];
  const count = () => {
    const n = checks.filter((c) => c.checked).length;
    $("#proposal-save").textContent = `Сохранить ${n} ${plural(n, "тренировку", "тренировки", "тренировок")}`;
    $("#proposal-save").disabled = !n;
  };
  checks.forEach((c) => c.addEventListener("change", count));
  count();
  $("#proposal-cancel").addEventListener("click", () => { state.proposal = null; renderProposal(); $("#plan-generate").textContent = "Составить план"; });
  $("#proposal-save").addEventListener("click", async (e) => {
    const items = checks.filter((c) => c.checked).map((c) => ({ ...pr.workouts[Number(c.dataset.index)], goal: pr.goal }));
    e.target.disabled = true;
    try {
      await api("/api/planned/bulk", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(items) });
    } catch (err) { banner(`Не удалось сохранить план: ${err.message}`, true); e.target.disabled = false; return; }
    state.proposal = null;
    renderProposal();
    $("#plan-generate").textContent = "Составить план";
    setPlanAi(false);
    banner(`В план добавлено: ${items.length} ${plural(items.length, "тренировка", "тренировки", "тренировок")}.`);
    await loadPlanned();
  });
}

(function initPlan() {
  const tomorrow = new Date(); tomorrow.setDate(tomorrow.getDate() + 1);
  $("#plan-start").value = isoDay(tomorrow);
  renderPlanAiSports();
  loadPlanned();
})();

// --- настройки ---------------------------------------------------------------

function renderSettings() {
  const s = state.status;
  $("#hrmax").value = s.hr_max;
  let html;
  if (!s.polar_configured) {
    html = `<p class="muted">Не заданы POLAR_CLIENT_ID / POLAR_CLIENT_SECRET в файле .env. Инструкция — в README.</p>`;
  } else if (!s.polar_connected) {
    const name = s.users?.find((u) => u.id === s.user)?.name || "";
    html = `<p class="muted">Ключи заданы. Осталось разрешить доступ к аккаунту Polar${name ? ` пользователя ${name}` : ""}.
            Если в Polar Flow в этом браузере вошёл другой человек, сначала выйдите из его аккаунта на flow.polar.com.</p>
            <a class="btn" href="/polar/connect">Подключить Polar</a>`;
  } else {
    html = `<p>Подключено${s.last_sync ? ` · последняя синхронизация ${fmtDate(s.last_sync, true)}` : ""}.</p>
            <p class="muted">API отдаёт тренировки за последние 30 дней, загруженные после подключения.</p>
            <div class="row"><button class="btn" id="sync-btn">Синхронизировать</button>
            <button class="btn secondary" id="polar-off">Отключить</button></div>`;
  }
  $("#polar-box").innerHTML = html;
  $("#sync-btn")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await api("/api/sync", { method: "POST" });
      banner(`Синхронизация: новых ${r.new}, обновлено ${r.updated}.`);
      await loadAll();
    } catch (err) { banner(`Ошибка синхронизации: ${err.message}`, true); }
    e.target.disabled = false;
  });
  $("#polar-off")?.addEventListener("click", async () => {
    await api("/api/polar/disconnect", { method: "POST" });
    await loadAll();
  });
}

$("#import-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const files = $("#import-files").files;
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  const r = await api("/api/import", { method: "POST", body: fd });
  $("#import-result").textContent = `Новых: ${r.new}, обновлено: ${r.updated}` + (r.errors.length ? `. Ошибки: ${r.errors.join("; ")}` : "");
  await loadAll();
});

$("#hrmax-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hr_max: Number($("#hrmax").value) }),
    });
    banner("HRmax сохранён, зоны пересчитаны.");
    await loadAll();
  } catch (err) { banner(`Ошибка: ${err.message}`, true); }
});

$("#demo-add").addEventListener("click", async () => {
  const r = await api("/api/demo", { method: "POST" });
  banner(`Добавлено демо-тренировок: ${r.created}.`);
  await loadAll();
});
$("#demo-del").addEventListener("click", async () => {
  const r = await api("/api/demo", { method: "DELETE" });
  banner(`Удалено демо-тренировок: ${r.deleted}.`);
  await loadAll();
});

// --- старт -------------------------------------------------------------------

(function init() {
  const params = new URLSearchParams(location.search);
  if (params.get("polar") === "connected") banner("Polar подключён. Нажмите «Синхронизировать».");
  if (params.get("polar") === "error") banner(`Не удалось подключить Polar (${params.get("msg")}).`, true);
  if (params.has("polar")) history.replaceState(null, "", "/");
  let view = params.has("polar") ? "settings" : "overview";
  try { view = params.has("polar") ? view : localStorage.getItem("view") || view; } catch {}
  show(view);
  loadAll().catch((e) => banner(`Ошибка загрузки: ${e.message}`, true));
  loadChatSessions();
  // Смена системной темы: перерисовать графики с новыми цветами токенов.
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    renderOverview();
    if (state.selected) openWorkout(state.selected);
  });
})();
