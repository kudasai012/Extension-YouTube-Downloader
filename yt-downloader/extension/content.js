/* =====================================================================
   YT Downloader — content script
   Рисует кнопку слева от "лайка", показывает меню качеств с примерным
   весом и отправляет запрос на локальный сервер (yt-dlp).
   ===================================================================== */

const SERVER = "http://127.0.0.1:5001";
const BTN_ID = "ytdl-download-btn";

/* ---------- утилиты ---------- */

function currentVideoUrl() {
  // Shorts: URL вида /shorts/VIDEO_ID
  const m = location.pathname.match(/^\/shorts\/([\w-]+)/);
  if (m) return `https://www.youtube.com/shorts/${m[1]}`;
  // Обычное видео: ?v=VIDEO_ID
  const id = new URLSearchParams(location.search).get("v");
  return id ? `https://www.youtube.com/watch?v=${id}` : location.href;
}

function currentVideoTitle() {
  // Заголовок видео со страницы (для имени файла)
  const h1 =
    document.querySelector("h1.ytd-watch-metadata yt-formatted-string") ||
    document.querySelector("h1.title yt-formatted-string") ||
    // Shorts: заголовок в оверлее ролика
    document.querySelector("ytd-reel-video-renderer .ytShortsVideoTitleViewModelShortsVideoTitle") ||
    document.querySelector("h1");
  let t = (h1 && h1.textContent.trim()) || document.title.replace(" - YouTube", "");
  return t.replace(/[\\/:*?"<>|]/g, "").slice(0, 150) || "video";
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  if (props.style) node.style.cssText = props.style;
  for (const c of children) node.append(c);
  return node;
}

/* ---------- запросы к серверу ---------- */

async function fetchFormats(url) {
  const r = await fetch(`${SERVER}/formats`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  if (!r.ok) throw new Error((await r.json()).error || r.statusText);
  return r.json();
}

async function startDownload(url, height) {
  const r = await fetch(`${SERVER}/download`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, height }),
  });
  if (!r.ok) throw new Error((await r.json()).error || r.statusText);
  return (await r.json()).job_id;
}

async function pollProgress(jobId, onUpdate) {
  return new Promise((resolve) => {
    const iv = setInterval(async () => {
      try {
        const r = await fetch(`${SERVER}/progress?job_id=${jobId}`);
        const p = await r.json();
        onUpdate(p);
        if (p.status === "done" || p.status === "error") {
          clearInterval(iv);
          resolve(p);
        }
      } catch (e) {
        clearInterval(iv);
        resolve({ status: "error", error: "Сервер недоступен" });
      }
    }, 600);
  });
}

/* ---------- меню выбора качества ---------- */

function closeMenu() {
  document.getElementById("ytdl-menu")?.remove();
  document.removeEventListener("click", onDocClick, true);
}

function onDocClick(e) {
  const menu = document.getElementById("ytdl-menu");
  const btn = document.getElementById(BTN_ID);
  if (menu && !menu.contains(e.target) && btn && !btn.contains(e.target)) {
    closeMenu();
  }
}

async function openMenu(anchor) {
  closeMenu();

  const menu = el("div", { id: "ytdl-menu", className: "ytdl-menu" });
  const rect = anchor.getBoundingClientRect();
  menu.style.cssText = `top:${rect.bottom + window.scrollY + 8}px;left:${rect.left + window.scrollX}px;`;

  menu.append(el("div", { className: "ytdl-menu-title", textContent: "Загрузка..." }));
  document.body.append(menu);
  setTimeout(() => document.addEventListener("click", onDocClick, true), 0);

  let data;
  try {
    data = await fetchFormats(currentVideoUrl());
  } catch (e) {
    menu.innerHTML = "";
    menu.append(el("div", { className: "ytdl-menu-title", textContent: "Ошибка" }));
    menu.append(el("div", {
      className: "ytdl-err",
      textContent:
        "Не удалось получить качества. Запущен ли локальный сервер? (" + e.message + ")",
    }));
    return;
  }

  menu.innerHTML = "";
  menu.append(el("div", { className: "ytdl-menu-title", textContent: "Выберите качество" }));

  // минимальное разрешение — 360p (на случай старого сервера)
  const qualities = (data.qualities || []).filter((q) => q.height >= 360);

  for (const q of qualities) {
    const row = el("button", { className: "ytdl-quality" });
    row.append(el("span", { className: "ytdl-q-res", textContent: q.label }));
    row.append(el("span", { className: "ytdl-q-size", textContent: q.size_human }));
    row.addEventListener("click", () => runDownload(menu, q.height, q.label));
    menu.append(row);
  }

  menu.append(el("div", {
    className: "ytdl-foot",
    textContent: "Скачивается в папку Downloads/YouTube",
  }));
}

async function runDownload(menu, height, label) {
  menu.innerHTML = "";
  menu.append(el("div", { className: "ytdl-menu-title", textContent: `Скачивание ${label}` }));

  const barWrap = el("div", { className: "ytdl-bar-wrap" });
  const bar = el("div", { className: "ytdl-bar" });
  barWrap.append(bar);
  menu.append(barWrap);

  const stat = el("div", { className: "ytdl-stat", textContent: "Старт..." });
  menu.append(stat);

  // Запускаем скачивание на сервере (yt-dlp качает + склеивает в Downloads/YouTube)
  let jobId;
  try {
    jobId = await startDownload(currentVideoUrl(), height);
  } catch (e) {
    stat.textContent = "Ошибка: " + e.message;
    return;
  }

  // Показываем прогресс серверной загрузки внутри меню
  const res = await pollProgress(jobId, (p) => {
    if (p.status === "downloading") {
      bar.style.width = (p.percent || 0) + "%";
      stat.textContent = `${p.percent || 0}%  ·  ${p.speed || ""}`;
    } else if (p.status === "processing") {
      bar.style.width = "100%";
      stat.textContent = "Склейка видео и аудио...";
    }
  });

  if (res.status === "done") {
    bar.style.width = "100%";
    bar.classList.add("ytdl-bar-ok");
    stat.textContent = "✅ Готово: " + (res.filename || "");

    // Кнопка «открыть папку» на ПК (на всякий случай)
    const actions = el("div", { className: "ytdl-actions" });
    const openF = el("button", {
      className: "ytdl-act-btn",
      textContent: "📂 Открыть папку",
    });
    let openBusy = false;
    openF.addEventListener("click", () => {
      // защита от двойных кликов: пока запрос в пути — игнорируем повторные
      if (openBusy) return;
      openBusy = true;
      openF.disabled = true;
      fetch(`${SERVER}/open_folder`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobId }),
      })
        .catch(() => {})
        .finally(() => {
          // короткая пауза, чтобы быстрые повторные клики не плодили окна
          setTimeout(() => {
            openBusy = false;
            openF.disabled = false;
          }, 700);
        });
    });
    actions.append(openF);
    menu.append(actions);
  } else {
    stat.textContent = "❌ Ошибка: " + (res.error || "неизвестно");
  }
}

/* ---------- иконка скачивания (SVG) ---------- */

function downloadIcon() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "24");
  svg.setAttribute("height", "24");
  svg.setAttribute("focusable", "false");
  const path = document.createElementNS(ns, "path");
  // Иконка скачивания в стиле YouTube (стрелка вниз + лоток)
  path.setAttribute(
    "d",
    "M17 18v1H6v-1h11zm-.5-6.6-.7-.7-3.3 3.29V4h-1v9.99L7.2 10.7l-.7.7 4.5 4.5 4.5-4.5z"
  );
  path.setAttribute("fill", "currentColor");
  svg.append(path);
  return svg;
}

/* ---------- кнопки ---------- */

// Обычная кнопка-чип (для /watch) — слева от лайка, с текстом
function buildButton() {
  const btn = el("button", { id: BTN_ID, className: "ytdl-btn", title: "Скачать видео" });
  btn.append(downloadIcon());
  btn.append(el("span", { className: "ytdl-btn-text", textContent: "Скачать" }));
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    openMenu(btn);
  });
  return btn;
}

// Круглая вертикальная кнопка (для Shorts) — над лайком, без текста
function buildShortsButton() {
  const wrap = el("div", { id: BTN_ID, className: "ytdl-shorts-btn", title: "Скачать видео" });
  const circle = el("button", { className: "ytdl-shorts-circle" });
  circle.append(downloadIcon());
  wrap.append(circle);
  wrap.append(el("span", { className: "ytdl-shorts-label", textContent: "Скачать" }));
  circle.addEventListener("click", (e) => {
    e.stopPropagation();
    openMenu(wrap);
  });
  return wrap;
}

function isShorts() {
  return location.pathname.startsWith("/shorts");
}

/* ---------- вставка в обычном /watch ---------- */

function insertWatchButton() {
  const actions =
    document.querySelector("#top-level-buttons-computed") ||
    document.querySelector("ytd-menu-renderer #top-level-buttons-computed");
  if (!actions) return false;

  const likeSegment =
    actions.querySelector("segmented-like-dislike-button-view-model") ||
    actions.querySelector("ytd-segmented-like-dislike-button-renderer") ||
    actions.firstElementChild;

  const btn = buildButton();
  if (likeSegment) actions.insertBefore(btn, likeSegment);
  else actions.prepend(btn);
  return true;
}

/* ---------- вставка в Shorts (над лайком) ---------- */

function isElementVisible(elm) {
  if (!elm) return false;
  const r = elm.getBoundingClientRect();
  return (
    r.width > 0 &&
    r.height > 0 &&
    r.bottom > 0 &&
    r.top < window.innerHeight
  );
}

// Находит кнопку "лайк" в текущем видимом Shorts (по aria/id/href — живуче)
function findShortsLikeButton() {
  // 1) пробуем по разным селекторам, которые встречались в вёрстке Shorts
  const candidates = [
    "#like-button",
    "ytd-toggle-button-renderer",
    "ytd-like-button-renderer",
    "like-button-view-model",
    "button[aria-label*='Нравится']",
    "button[aria-label*='like' i]",
    "[id='like-button']",
  ];
  const likes = [];
  for (const sel of candidates) {
    document.querySelectorAll(sel).forEach((n) => likes.push(n));
  }
  // оставляем только видимые
  const visible = likes.filter(isElementVisible);
  if (!visible.length) return null;
  // берём самый верхний (в Shorts лайк — первая кнопка в боковой панели)
  visible.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  return visible[0];
}

function insertShortsButton() {
  const likeBtn = findShortsLikeButton();
  if (!likeBtn) return false;

  // Контейнер кнопки лайка — поднимаемся до "ячейки" действия (родитель,
  // который стоит в вертикальном ряду). Вставляем нашу кнопку ПЕРЕД ней.
  // Ищем родителя, который является прямым ребёнком панели действий.
  let likeCell = likeBtn;
  // поднимаемся максимум на несколько уровней, пока родитель не станет
  // контейнером с несколькими кнопками (панель действий)
  for (let i = 0; i < 6 && likeCell.parentElement; i++) {
    const p = likeCell.parentElement;
    // панель действий обычно содержит несколько кнопок-ячеек
    if (p.childElementCount >= 2 && p.querySelectorAll("button").length >= 2) {
      const btn = buildShortsButton();
      p.insertBefore(btn, likeCell);
      return true;
    }
    likeCell = p;
  }
  // запасной вариант: вставить прямо перед кнопкой лайка
  const btn = buildShortsButton();
  likeBtn.parentElement.insertBefore(btn, likeBtn);
  return true;
}

function insertButton() {
  if (isShorts()) {
    // На Shorts активный ролик меняется при прокрутке — кнопка должна быть
    // в видимой панели. Если она "осела" в скрытом ролике — переставим.
    const existing = document.getElementById(BTN_ID);
    if (existing) {
      const r = existing.getBoundingClientRect();
      const visible =
        r.bottom > 0 && r.top < window.innerHeight && r.width > 0 && r.height > 0;
      if (visible) return; // уже в нужном месте
      existing.remove(); // была в скрытом ролике — убираем и ставим заново
    }
    insertShortsButton();
  } else if (location.pathname.startsWith("/watch")) {
    if (document.getElementById(BTN_ID)) return;
    insertWatchButton();
  } else {
    // ушли со страницы видео/Shorts — подчистим кнопку
    document.getElementById(BTN_ID)?.remove();
  }
}

/* ---------- наблюдатель: YouTube — SPA, контент меняется без перезагрузки ---------- */

const observer = new MutationObserver(() => insertButton());
observer.observe(document.documentElement, { childList: true, subtree: true });

// На навигацию внутри YouTube
document.addEventListener("yt-navigate-finish", () => {
  closeMenu();
  setTimeout(insertButton, 300);
});

insertButton();
