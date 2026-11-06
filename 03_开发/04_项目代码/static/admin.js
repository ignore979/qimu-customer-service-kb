// 栖木家居客服知识库 —— 索引管理页交互
(function () {
  "use strict";

  const indexStatusEl = document.getElementById("indexStatus");
  const dropEl = document.getElementById("drop");
  const fileInput = document.getElementById("fileInput");
  const chooseBtn = document.getElementById("chooseBtn");
  const msgEl = document.getElementById("msg");

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function showMsg(kind, text) {
    msgEl.hidden = false;
    msgEl.className = "msg " + kind;
    msgEl.textContent = text;
  }

  function renderStatus(index) {
    if (!index || !index.ready) {
      indexStatusEl.innerHTML =
        '<div class="empty-state"><span class="icon">📭</span>索引尚未建立，请先导入资料。</div>';
      return;
    }
    const topics = index.topics || {};
    const pageRange = index.page_range ? index.page_range.join(" — ") + " 页" : "—";
    const sections = (index.sections || [])
      .map(function (s) { return "<li>" + escapeHtml(s) + "</li>"; })
      .join("");

    indexStatusEl.innerHTML =
      '<dl class="kv">' +
      "<dt>索引来源</dt><dd>" + escapeHtml(index.source || "—") + "</dd>" +
      "<dt>资料版本</dt><dd>" + escapeHtml(index.version || "—") + "</dd>" +
      "<dt>正文条目数</dt><dd>" + (index.entry_count || 0) + " 条</dd>" +
      "<dt>页码范围</dt><dd>" + escapeHtml(pageRange) + "</dd>" +
      "</dl>" +
      '<div class="topic-row">' +
      '<span class="topic-chip">QM-D 产品资料：' + (topics["QM-D"] || 0) + "</span>" +
      '<span class="topic-chip">QM-P 使用维护：' + (topics["QM-P"] || 0) + "</span>" +
      '<span class="topic-chip">QM-S 售后客服：' + (topics["QM-S"] || 0) + "</span>" +
      "</div>" +
      (sections
        ? '<ul class="sections">' + sections + "</ul>"
        : '<p class="desc" style="margin-top:10px">无章节信息。</p>');
  }

  async function loadStatus() {
    try {
      const resp = await fetch("/api/index/status");
      const data = await resp.json();
      renderStatus(data.index);
    } catch (err) {
      indexStatusEl.innerHTML =
        '<div class="empty-state"><span class="icon">⚠</span>读取索引状态失败：' +
        escapeHtml(err.message) +
        "</div>";
    }
  }

  async function importFile(file) {
    if (!file) return;
    showMsg("ok", "正在解析并重建索引：" + file.name + " …");
    try {
      const buf = await file.arrayBuffer();
      const resp = await fetch("/api/import", {
        method: "POST",
        headers: {
          "Content-Type": "application/octet-stream",
          "X-File-Name": file.name,
        },
        body: buf,
      });
      const data = await resp.json();
      if (!resp.ok || data.status === "error") {
        showMsg("err", data.message || "导入失败，请稍后重试。");
        return;
      }
      showMsg("ok", data.message || "导入成功。");
      renderStatus(data.index);
    } catch (err) {
      showMsg("err", "导入失败：" + err.message);
    }
  }

  chooseBtn.addEventListener("click", function () {
    fileInput.click();
  });

  fileInput.addEventListener("change", function () {
    importFile(fileInput.files[0]);
    fileInput.value = "";
  });

  dropEl.addEventListener("dragover", function (e) {
    e.preventDefault();
    dropEl.classList.add("dragover");
  });

  dropEl.addEventListener("dragleave", function () {
    dropEl.classList.remove("dragover");
  });

  dropEl.addEventListener("drop", function (e) {
    e.preventDefault();
    dropEl.classList.remove("dragover");
    if (e.dataTransfer && e.dataTransfer.files.length) {
      importFile(e.dataTransfer.files[0]);
    }
  });

  loadStatus();
})();
