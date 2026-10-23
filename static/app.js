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
    throw new Error(body.detail || `HTTP ${r.status}`);
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
  renderSettings();
  if (!state.workouts.length) banner("Пока нет тренировок. Подключите Polar, импортируйте файлы или добавьте демо-данные в разделе «Данные и настройки».");
}

function renderStatus() {
  const s = state.status;
  const parts = [`${s.workouts} тренировок`];
  if (s.polar_connected) parts.push("Polar подключён");
  if (s.last_sync) parts.push(`синхр. ${fmtDate(s.last_sync, true)}`);
  $("#sync-status").textContent = parts.join(" · ");
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
    chart("chart-workout", {
      type: "line",
      data: { datasets: [{ label: "Пульс", data: pts, borderColor: css("--chart-1"), borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, tension: 0.2 }] },
      options: {
        ...baseOpts({ legend: false, tooltip: { callbacks: { title: (c) => `${Math.round(c[0].parsed.x)} мин` } } }),
        parsing: false,
        scales: {
          x: { type: "linear", border: { display: false }, ticks: { color: css("--text-muted"), font: { size: 12 }, callback: (v) => `${Math.round(v)} мин` }, grid: { display: false } },
          y: { border: { display: false }, ticks: { color: css("--text-muted"), font: { size: 12 } }, grid: { color: css("--border") } },
        },
      },
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

async function sendChat(text) {
  text = text.trim();
  if (!text) return;
  $("#chat-input").value = "";
  autoGrow();
  $("#chat-send").disabled = true;
  addMsg("user", text);
  const out = addMsg("ai");
  out.classList.add("pending");
  let acc = "";
  try {
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: state.conversationId, message: text, sports: chatScopeSports(), days: state.chatScope.days }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      acc += dec.decode(value, { stream: true });
      renderMarkdown(out, acc);
      out.scrollIntoView({ block: "end" });
    }
    if (!acc.trim()) {
      acc = "⚠️ Сервер вернул пустой ответ. Посмотрите ошибку в окне, где запущен сервер.";
      renderMarkdown(out, acc);
    }
  } catch (e) {
    acc += `\n\n⚠️ ${e.message}`;
    renderMarkdown(out, acc);
  } finally {
    out.classList.remove("pending");
    $("#chat-send").disabled = false;
  }
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
$("#chat-reset").addEventListener("click", async () => {
  await fetch(`/api/chat/${state.conversationId}`, { method: "DELETE" });
  state.conversationId = newId();
  $("#chat-log").innerHTML = `<div class="chat-empty" id="chat-empty"><p>Новый диалог. Спросите что-нибудь о своих тренировках.</p></div>`;
});

// --- настройки ---------------------------------------------------------------

function renderSettings() {
  const s = state.status;
  $("#hrmax").value = s.hr_max;
  let html;
  if (!s.polar_configured) {
    html = `<p class="muted">Не заданы POLAR_CLIENT_ID / POLAR_CLIENT_SECRET в файле .env. Инструкция — в README.</p>`;
  } else if (!s.polar_connected) {
    html = `<p class="muted">Ключи заданы. Осталось разрешить доступ к вашему аккаунту Polar.</p>
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
  // Смена системной темы: перерисовать графики с новыми цветами токенов.
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    renderOverview();
    if (state.selected) openWorkout(state.selected);
  });
})();
