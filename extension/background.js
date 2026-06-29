/* Service worker.
   Скачивание выполняет локальный сервер (yt-dlp) в папку Downloads/YouTube.
   Здесь — только проверка доступности сервера для попапа. */

// В Chrome/Яндекс есть chrome.*, в некоторых сборках — browser.*
const api = typeof chrome !== "undefined" ? chrome : (typeof browser !== "undefined" ? browser : null);

const SERVER = "http://127.0.0.1:5001";

if (api && api.runtime && api.runtime.onMessage) {
  api.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg && msg.type === "ping") {
      fetch(`${SERVER}/ping`)
        .then((r) => r.json())
        .then((d) => sendResponse({ ok: true, data: d }))
        .catch(() => sendResponse({ ok: false }));
      return true; // async
    }
  });
} else {
  console.error("[YTDL] chrome.runtime недоступен — проверь manifest_version: 3");
}
