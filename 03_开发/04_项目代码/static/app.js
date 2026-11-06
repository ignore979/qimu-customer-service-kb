// 栖木家居客服知识库 —— 前端交互
(function () {
  "use strict";

  const form = document.getElementById("searchForm");
  const input = document.getElementById("questionInput");
  const searchBtn = document.getElementById("searchBtn");
  const clearBtn = document.getElementById("clearBtn");
  const statusBar = document.getElementById("statusBar");
  const resultsEl = document.getElementById("results");

  // 与后端 MAX_QUESTION_LEN 保持一致，做前端预校验
  const MAX_QUESTION_LEN = 500;

  // 相关度分级：0.6 以上高相关，0.3~0.6 中等，0.3 以下低相关
  function scoreLevel(score) {
    if (score >= 0.6) return "high";
    if (score >= 0.3) return "mid";
    return "low";
  }

  function scoreText(score) {
    return "相关度 " + (score * 100).toFixed(0) + "%";
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function setStatus(kind, message) {
    statusBar.hidden = false;
    statusBar.className = "status-bar " + kind;
    statusBar.textContent = message;
  }

  function clearStatus() {
    statusBar.hidden = true;
    statusBar.textContent = "";
    statusBar.className = "status-bar";
  }

  function renderEmpty(text, icon) {
    resultsEl.innerHTML =
      '<div class="empty-state"><span class="icon">' +
      (icon || "🔍") +
      "</span>" +
      escapeHtml(text) +
      "</div>";
  }

  function qmBadges(item) {
    const qms = Array.isArray(item.qm) ? item.qm : item.qm ? [item.qm] : [];
    return qms
      .map(function (qm) {
        return '<span class="qm-badge">' + escapeHtml(qm) + "</span>";
      })
      .join("");
  }

  function renderResultCard(item) {
    const level = scoreLevel(item.score);
    const versionBadge = item.version
      ? '<span class="version-badge">资料版本 ' + escapeHtml(item.version) + "</span>"
      : "";
    const pageBadge = item.page
      ? '<span class="page-badge">第 ' + item.page + " 页</span>"
      : "";
    const scoreBadge =
      '<span class="score-badge ' +
      level +
      '">' +
      escapeHtml(scoreText(item.score)) +
      "</span>";

    // 只展示条目标题（单一主题），不展示 OSS 文件路径（PRD：路径不得泄露）
    const source =
      '<div class="result-source">来源条目：' +
      escapeHtml(item.title || item.qm || "知识库条目") +
      "</div>";

    return (
      '<article class="result-card ' +
      (level === "low" ? "low" : "") +
      '">' +
      '<div class="result-card-top">' +
      qmBadges(item) +
      pageBadge +
      versionBadge +
      scoreBadge +
      "</div>" +
      '<div class="result-content">' +
      escapeHtml(item.content || "（该片段内容为空）") +
      "</div>" +
      source +
      "</article>"
    );
  }

  function renderAnswer(answer, unconfirmed) {
    if (!answer) return "";
    const cls = unconfirmed ? "answer-card answer-card-unconfirmed" : "answer-card";
    const title = unconfirmed ? "⚠ 无法确认" : "💡 简短回答";
    return (
      '<section class="' + cls + '">' +
      '<div class="answer-card-title">' + title + "</div>" +
      '<div class="answer-card-body">' + escapeHtml(answer) + "</div>" +
      "</section>"
    );
  }

  function renderResults(data) {
    const results = data.results || [];
    const unconfirmed = !!data.insufficient;
    const answerBlock = renderAnswer(data.answer, unconfirmed);

    if (unconfirmed) {
      setStatus(
        "insufficient",
        "⚠ 现有资料可能未覆盖该问题，请向用户补充必要信息（如型号、订单状态、使用场景），不要凭猜测作答。"
      );
    } else {
      clearStatus();
    }

    if (results.length === 0) {
      // 无结果但有「无法确认」指引时，展示指引卡片而非空状态
      if (answerBlock) {
        resultsEl.innerHTML = answerBlock;
      } else {
        renderEmpty("未在知识库中找到相关内容，请尝试更换表述或补充产品型号。", "📭");
      }
      return;
    }

    const total = data.total || 0;
    const shown = data.shown || results.length;
    const header =
      '<p class="result-header">' +
      (answerBlock ? "以下为回答依据的检索内容：" : "共检索到 ") +
      (answerBlock
        ? ""
        : total > shown
          ? total + " 条相关内容，展示前 " + shown + " 条："
          : shown + " 条相关内容：") +
      "</p>";

    resultsEl.innerHTML =
      answerBlock + header + results.map(renderResultCard).join("");
  }

  async function doSearch(question) {
    question = (question || input.value || "").trim();

    if (!question) {
      // BUG-07：空输入时清空上一次结果，避免旧结果残留误导
      setStatus("error", "请输入要查询的问题。");
      resultsEl.innerHTML = "";
      input.focus();
      return;
    }

    if (question.length > MAX_QUESTION_LEN) {
      // BUG-06：前端先行拦截超长输入
      setStatus(
        "error",
        "问题过长（" + question.length + " 字符），请精简到 " + MAX_QUESTION_LEN + " 字符以内。"
      );
      resultsEl.innerHTML = "";
      input.focus();
      return;
    }

    searchBtn.disabled = true;
    searchBtn.innerHTML = '<span class="spinner"></span>检索中…';
    clearStatus();
    resultsEl.innerHTML =
      '<div class="empty-state"><span class="icon">⏳</span>正在检索知识库…</div>';

    try {
      const resp = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: question }),
      });
      const data = await resp.json();

      if (data.status === "rejected") {
        setStatus("rejected", data.message || "系统不处理敏感信息。");
        resultsEl.innerHTML = "";
        return;
      }

      if (data.status === "error" || !resp.ok) {
        setStatus("error", data.message || "检索失败，请稍后重试。");
        resultsEl.innerHTML = "";
        return;
      }

      renderResults(data);
    } catch (err) {
      setStatus("error", "网络请求失败：" + err.message);
      resultsEl.innerHTML = "";
    } finally {
      searchBtn.disabled = false;
      searchBtn.textContent = "检索知识库";
    }
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    doSearch();
  });

  clearBtn.addEventListener("click", function () {
    input.value = "";
    clearStatus();
    resultsEl.innerHTML = "";
    input.focus();
  });

  // 快捷示例
  document.querySelectorAll(".chip").forEach(function (chip) {
    chip.addEventListener("click", function () {
      input.value = chip.getAttribute("data-q") || "";
      doSearch();
    });
  });

  // Ctrl+Enter 快捷提交
  input.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      doSearch();
    }
  });

  // 初始提示
  renderEmpty(
    "请输入客服收到的用户问题，点击“检索知识库”定位相关资料依据。",
    "💬"
  );
})();
