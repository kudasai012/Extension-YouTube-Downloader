const api = typeof chrome !== 'undefined' ? chrome : browser;
const SERVER = 'http://127.0.0.1:5001';

async function pingServer() {
  const r = await fetch(`${SERVER}/ping`);
  if (!r.ok) throw new Error('server not reachable');
  return r.json();
}

async function cleanupTempFile(jobId) {
  try {
    await fetch(`${SERVER}/cleanup?job_id=${encodeURIComponent(jobId)}`, { method: 'POST' });
  } catch (e) {
    /* сервер мог быть уже остановлен — временный файл просто останется на диске */
  }
}

function extractJobId(downloadUrl) {
  try {
    const u = new URL(downloadUrl);
    if (!u.pathname.endsWith('/file')) return null;
    return u.searchParams.get('job_id');
  } catch (e) {
    return null;
  }
}

// Когда браузер полностью сохранил файл в "Загрузки" (Ctrl+J) — удаляем
// временную копию с диска сервера (Downloads/YouTube/.tmp_browser_downloads).
// job_id достаём прямо из URL самой загрузки — так это работает и для
// скачивания через api.downloads.download(), и для резервного пути через
// обычную ссылку <a download> в content.js.
api.downloads.onChanged.addListener((delta) => {
  if (!delta.state || delta.state.current !== 'complete') return;
  api.downloads.search({ id: delta.id }, (items) => {
    const item = items && items[0];
    if (!item || !item.url) return;
    const jobId = extractJobId(item.url);
    if (jobId) cleanupTempFile(jobId);
  });
});

api.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === 'ping_server') {
    pingServer()
      .then((data) => sendResponse({ ok: true, data }))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  }

  if (msg?.type === 'browser_download') {
    const url = `${SERVER}/file?job_id=${encodeURIComponent(msg.job_id)}`;
    api.downloads.download(
      {
        url,
        saveAs: false,
        conflictAction: 'uniquify'
      },
      (downloadId) => {
        if (api.runtime.lastError) {
          sendResponse({ ok: false, error: api.runtime.lastError.message });
          return;
        }
        sendResponse({ ok: true, downloadId });
      }
    );
    return true;
  }
});