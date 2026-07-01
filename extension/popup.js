const dot = document.getElementById("dot");
const statusText = document.getElementById("status-text");
const serverHint = document.getElementById("server-hint");
const retryBtn = document.getElementById("retry");

function check() {
  dot.className = "dot";
  statusText.textContent = "Проверка сервера…";

  chrome.runtime.sendMessage({ type: "ping_server" }, (res) => {
    if (chrome.runtime.lastError || !res || !res.ok) {
      dot.classList.add("off");
      statusText.textContent = "Сервер не запущен";
      serverHint.style.display = "";
      return;
    }
    dot.classList.add("on");
    statusText.textContent = "Сервер запущен";
    serverHint.style.display = "none";
  });
}

retryBtn.addEventListener("click", check);
check();
