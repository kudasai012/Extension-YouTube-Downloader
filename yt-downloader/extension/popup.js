const dot = document.getElementById("dot");
const statusText = document.getElementById("status-text");
const saveDir = document.getElementById("save-dir");

chrome.runtime.sendMessage({ type: "ping" }, (res) => {
  if (res && res.ok) {
    dot.classList.add("on");
    statusText.textContent = "Сервер запущен ✓";
    if (res.data && res.data.save_dir) {
      saveDir.textContent = "Папка: " + res.data.save_dir;
    }
  } else {
    dot.classList.add("off");
    statusText.textContent = "Сервер не запущен";
  }
});
