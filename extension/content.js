/* =====================================================================
   YT Downloader — content script
   Рисует кнопку слева от "лайка", показывает меню качеств с примерным
   весом и отправляет запрос на локальный сервер (yt-dlp).
   ===================================================================== */

const SERVER = "http://127.0.0.1:5001";
const WATCH_BTN_ID = "ytdl-watch-download-btn";
const SHORTS_BTN_ID = "ytdl-shorts-download-btn";

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

function currentAnchorButton() {
  return (
    document.getElementById(WATCH_BTN_ID) ||
    document.getElementById(SHORTS_BTN_ID)
  );
}

function onDocClick(e) {
  const menu = document.getElementById("ytdl-menu");
  const btn = currentAnchorButton();
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
function buildWatchButton() {
  const btn = el("button", {
    id: WATCH_BTN_ID,
    className: "ytdl-watch-btn",
    title: "Скачать видео",
  });
  btn.append(downloadIcon());
  btn.append(el("span", { className: "ytdl-watch-btn-text", textContent: "Скачать" }));
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    openMenu(btn);
  });
  return btn;
}

// Полностью отдельная кнопка для Shorts
function buildShortsButton() {
  const wrap = el("div", {
    id: SHORTS_BTN_ID,
    className: "ytdl-shorts-btn",
    title: "Скачать видео",
  });
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

  const btn = buildWatchButton();
  if (likeSegment) actions.insertBefore(btn, likeSegment);
  else actions.prepend(btn);
  return true;
}

/* ---------- вставка в Shorts (над лайком) ---------- */

function isElementVisible(elm) {
  if (!elm) return false;
  const r = elm.getBoundingClientRect();
  return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
}

// Находит видимую кнопку "лайк" в текущем Shorts
function findShortsLikeButton() {
  const sels = [
    "#like-button",
    "ytd-toggle-button-renderer",
    "ytd-like-button-renderer",
    "like-button-view-model",
    "button[aria-label*='Нравится']",
    "button[aria-label*='like' i]",
  ];
  const found = [];
  for (const s of sels) document.querySelectorAll(s).forEach((n) => found.push(n));
  const vis = found.filter(isElementVisible);
  if (!vis.length) return null;
  vis.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
  return vis[0];
}

// Поднимается от кнопки лайка до её "ячейки" — прямого ребёнка панели действий.
// Так наша кнопка встанет ровно в тот же ряд, что и нативные.
function findShortsActionCell(likeBtn) {
  let node = likeBtn;
  let parent = node.parentElement;
  // ищем родителя-контейнер, где лежат несколько вертикальных ячеек действий
  for (let i = 0; i < 8 && parent; i++) {
    const siblings = parent.children;
    // панель действий: несколько детей, расположенных вертикально (столбиком)
    if (siblings.length >= 2) {
      const a = node.getBoundingClientRect();
      let verticalSiblings = 0;
      for (const sib of siblings) {
        if (sib === node) continue;
        const b = sib.getBoundingClientRect();
        // другой элемент примерно по той же вертикали (столбик действий)
        if (b.width && Math.abs((b.left + b.width / 2) - (a.left + a.width / 2)) < 40) {
          verticalSiblings++;
        }
      }
      if (verticalSiblings >= 1) {
        return { cell: node, panel: parent };
      }
    }
    node = parent;
    parent = node.parentElement;
  }
  return { cell: likeBtn, panel: likeBtn.parentElement };
}

function insertShortsButton() {
  const likeBtn = findShortsLikeButton();
  if (!likeBtn) return false;

  const { cell, panel } = findShortsActionCell(likeBtn);
  if (!panel) return false;

  const btn = buildShortsButton();
  panel.insertBefore(btn, cell); // НАД лайком
  return true;
}

function insertButton() {
  if (isShorts()) {
    // На Shorts оставляем ТОЛЬКО отдельную shorts-кнопку.

    // Активный ролик меняется при прокрутке — наша кнопка должна стоять рядом
    // с ВИДИМЫМ лайком. Сверяем: если наша кнопка далеко от текущего лайка —
    // переносим её.
    const like = findShortsLikeButton();
    if (!like) return;
    document.getElementById(WATCH_BTN_ID)?.remove();

    const existing = document.getElementById(SHORTS_BTN_ID);
    if (existing) {
      const er = existing.getBoundingClientRect();
      const lr = like.getBoundingClientRect();
      const sameColumn =
        er.width > 0 &&
        Math.abs((er.left + er.width / 2) - (lr.left + lr.width / 2)) < 60 &&
        er.bottom > 0 &&
        er.top < window.innerHeight;
      const isAboveLike = er.bottom <= lr.top + 8;
      if (sameColumn && isAboveLike) return; // уже на месте у активного ролика
      existing.remove(); // не на месте — переставим выше
    }
    insertShortsButton();
  } else if (location.pathname.startsWith("/watch")) {
    document.getElementById(SHORTS_BTN_ID)?.remove();
    if (document.getElementById(WATCH_BTN_ID)) return;
    insertWatchButton();
  } else {
    document.getElementById(WATCH_BTN_ID)?.remove();
    document.getElementById(SHORTS_BTN_ID)?.remove();
  }
}

/* ---------- наблюдатель: YouTube — SPA, контент меняется без перезагрузки ----------
   Throttle через requestAnimationFrame: на Shorts DOM меняется очень часто,
   без троттлинга insertButton() дёргался бы сотни раз в секунду — отсюда лаги
   и "прыжки" кнопки. Теперь — максимум один вызов за кадр. */

let scheduled = false;
function scheduleInsert() {
  if (scheduled) return;
  scheduled = true;
  requestAnimationFrame(() => {
    scheduled = false;
    try {
      insertButton();
    } catch (e) {
      /* ignore */
    }
  });
}

const observer = new MutationObserver(scheduleInsert);
observer.observe(document.documentElement, { childList: true, subtree: true });

// Навигация внутри YouTube (в т.ч. перелистывание Shorts)
document.addEventListener("yt-navigate-finish", () => {
  closeMenu();
  setTimeout(scheduleInsert, 300);
});

scheduleInsert();
