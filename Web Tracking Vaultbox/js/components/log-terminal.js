import { escapeHtml } from "../formatters.js";
import { icons } from "../icons.js";

function formatRichLogLine(line) {
  if (!line) return "";
  const raw = String(line);
  const lower = raw.toLowerCase();

  // 1. Whole-line Error check
  if (lower.startsWith("failed:") || lower.startsWith("error:") || lower.includes("exception:") || lower.includes("traceback")) {
    return `<div class="log-line log-error">${escapeHtml(raw)}</div>`;
  }

  // 2. Summary completion line check
  if (raw.startsWith("Done: ") || raw.startsWith("Completed: ") || raw.startsWith("Success: ")) {
    return `<div class="log-line log-line-summary">${escapeHtml(raw)}</div>`;
  }

  // 3. Prefix tag match e.g. [Final-Assembly], [Done], [Cleanup], [Backup-Resume], [IP-Guard], [Download], [Trace], [Download-Retry]
  const tagMatch = raw.match(/^\[([a-zA-Z0-9_\-.:/ ]+)\]\s*(.*)$/);
  if (tagMatch) {
    const tagName = tagMatch[1];
    const rest = tagMatch[2];
    const tagLower = tagName.toLowerCase();

    let tagClass = "log-tag-default";
    let lineClass = "";

    if (tagLower.includes("done") || tagLower === "complete" || tagLower === "completed") {
      tagClass = "log-tag-done";
      lineClass = "log-line-done";
    } else if (tagLower.includes("cleanup") || tagLower.includes("clean") || tagLower.includes("dọn dẹp")) {
      tagClass = "log-tag-cleanup";
      lineClass = "log-line-cleanup";
    } else if (tagLower.includes("retry") || tagLower.includes("warn") || tagLower.includes("thử lại")) {
      tagClass = "log-tag-retry";
      lineClass = "log-line-retry";
    } else if (tagLower.includes("error") || tagLower.includes("fail") || tagLower.includes("lỗi")) {
      tagClass = "log-tag-error";
      lineClass = "log-error";
    } else if (tagLower.includes("download") || tagLower.includes("tải xuống")) {
      tagClass = "log-tag-download";
      lineClass = "log-line-download";
    } else if (tagLower.includes("upload") || tagLower.includes("tải lên")) {
      tagClass = "log-tag-upload";
      lineClass = "log-line-upload";
    } else if (tagLower.includes("backup") || tagLower.includes("resume") || tagLower.includes("khôi phục")) {
      tagClass = "log-tag-backup";
      lineClass = "log-line-backup";
    } else if (tagLower.includes("assembly") || tagLower.includes("final") || tagLower.includes("ghép")) {
      tagClass = "log-tag-assembly";
      lineClass = "log-line-final";
    } else if (tagLower.includes("ip") || tagLower.includes("guard") || tagLower.includes("token")) {
      tagClass = "log-tag-ip";
      lineClass = "log-line-ip";
    } else if (tagLower.includes("trace")) {
      tagClass = "log-tag-trace";
      lineClass = "log-line-trace";
    } else if (tagLower.includes("success") || tagLower.includes("hoàn tất") || tagLower.includes("thành công")) {
      tagClass = "log-tag-success";
      lineClass = "log-line-done";
    }

    let formattedRest = escapeHtml(rest);

    // Highlight segments & percentages e.g. "4500/5894 segments (76%) | 206 MB tích lũy"
    formattedRest = formattedRest.replace(/(\(\d+%\)|\(\d+\.\d+%\))/g, '<span class="log-highlight-pct">$1</span>');
    formattedRest = formattedRest.replace(/(\d+(?:\.\d+)?\s*(?:MB|GB|KB|bytes|KB\/s|MB\/s))/gi, '<span class="log-highlight-size">$1</span>');
    formattedRest = formattedRest.replace(/(https?:\/\/[^\s'"]+)/g, '<span class="log-highlight-url">$1</span>');
    formattedRest = formattedRest.replace(/('410 Gone'|'403 Forbidden'|'404 Not Found'|'500 Internal Server Error')/gi, '<span class="log-highlight-code">$1</span>');

    return `<div class="log-line ${lineClass}"><span class="log-tag ${tagClass}">[${escapeHtml(tagName)}]</span>${formattedRest}</div>`;
  }

  // 4. Fallback index bracket [1/20]
  const indexedMatch = raw.match(/^(\[\d+\/\d+\])\s*(.*)$/);
  if (indexedMatch) {
    return `<div class="log-line"><span class="log-index">${escapeHtml(indexedMatch[1])}</span> ${escapeHtml(indexedMatch[2])}</div>`;
  }

  // 5. Standard line checks
  if (lower.includes("error") || lower.includes("fail") || lower.includes("lỗi")) {
    return `<div class="log-line log-error">${escapeHtml(raw)}</div>`;
  }
  if (lower.includes("completed") || lower.includes("success") || lower.includes("hoàn tất") || lower.includes("thành công")) {
    return `<div class="log-line log-success">${escapeHtml(raw)}</div>`;
  }
  if (lower.includes("warn") || lower.includes("retry") || lower.includes("cảnh báo")) {
    return `<div class="log-line log-warn">${escapeHtml(raw)}</div>`;
  }

  return `<div class="log-line">${escapeHtml(raw)}</div>`;
}

export class LogTerminalComponent {
  constructor(containerEl) {
    this.containerEl = containerEl;
    this.logs = [];
    this.autoScroll = true;
    this.searchQuery = "";
    this.levelFilter = "all"; // all, error, success, info
    this.isFullscreen = false;
  }

  setLogs(logs) {
    this.logs = Array.isArray(logs) ? logs : [];
    this.renderBody();
  }

  appendLog(line) {
    if (!line) return;
    this.logs.push(String(line));
    this.renderBody();
  }

  render() {
    this.containerEl.innerHTML = `
      <div class="colab-console-wrapper" id="terminalRoot">
        <div class="colab-console-header">
          <div class="console-title-group">
            <div class="console-window-dots">
              <span class="console-dot red"></span>
              <span class="console-dot yellow"></span>
              <span class="console-dot green"></span>
            </div>
            <span class="console-title" style="display: flex; align-items: center; gap: 5px;">
              ${icons.terminal("icon-sm")} &gt;_ Colab Execution Logs
            </span>
            <span id="logCountBadge" class="stat-chip" style="padding: 1px 7px; font-size: 10px; font-family: var(--font-mono);">
              ${this.logs.length} lines
            </span>
          </div>

          <div class="console-controls">
            <!-- Quick Level Filters -->
            <div class="console-filter-chips">
              <button class="console-filter-chip active" data-level="all">All</button>
              <button class="console-filter-chip" data-level="error">Errors</button>
              <button class="console-filter-chip" data-level="success">Done</button>
            </div>

            <!-- Search Field -->
            <div class="console-search-wrapper">
              <span class="console-search-icon">${icons.search("icon-sm")}</span>
              <input type="text" class="console-search-input" id="terminalSearchInput" placeholder="Filter logs..." value="${escapeHtml(this.searchQuery)}">
            </div>

            <!-- Auto Scroll -->
            <label class="console-scroll-label" title="Automatically scroll to latest logs">
              <input type="checkbox" class="console-scroll-input" id="autoScrollCb" ${this.autoScroll ? "checked" : ""}>
              <span>Scroll</span>
            </label>

            <!-- Action Buttons with SVG Icons -->
            <button class="btn btn-sm" id="copyLogsBtn" title="Copy all logs to clipboard">
              ${icons.copy("icon-sm")}
              <span>Copy</span>
            </button>
            <button class="btn btn-sm" id="downloadLogsBtn" title="Export logs as file">
              ${icons.download("icon-sm")}
              <span>Export</span>
            </button>
            <button class="btn btn-sm btn-icon-only" id="fullscreenBtn" title="Toggle Fullscreen">
              ${icons.maximize("icon-sm")}
            </button>
          </div>
        </div>

        <div class="colab-console-body" id="terminalBody"></div>
      </div>
    `;

    // Bind event handlers
    const searchInput = this.containerEl.querySelector("#terminalSearchInput");
    if (searchInput) {
      searchInput.addEventListener("input", (e) => {
        this.searchQuery = e.target.value.toLowerCase().trim();
        this.renderBody();
      });
    }

    const autoScrollCb = this.containerEl.querySelector("#autoScrollCb");
    if (autoScrollCb) {
      autoScrollCb.addEventListener("change", (e) => {
        this.autoScroll = e.target.checked;
        if (this.autoScroll) this.scrollToBottom();
      });
    }

    const filterChips = this.containerEl.querySelectorAll(".console-filter-chip");
    filterChips.forEach((chip) => {
      chip.addEventListener("click", () => {
        filterChips.forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
        this.levelFilter = chip.dataset.level || "all";
        this.renderBody();
      });
    });

    const copyBtn = this.containerEl.querySelector("#copyLogsBtn");
    if (copyBtn) {
      copyBtn.addEventListener("click", () => {
        const text = this.logs.join("\n");
        navigator.clipboard.writeText(text).then(() => {
          const originalHTML = copyBtn.innerHTML;
          copyBtn.innerHTML = `${icons.checkCircle("icon-sm")} <span>Copied ✓</span>`;
          copyBtn.style.color = "var(--ok)";
          setTimeout(() => {
            copyBtn.innerHTML = originalHTML;
            copyBtn.style.color = "";
          }, 2000);
        });
      });
    }

    const downloadBtn = this.containerEl.querySelector("#downloadLogsBtn");
    if (downloadBtn) {
      downloadBtn.addEventListener("click", () => {
        const text = this.logs.join("\n");
        const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `vaultbox_colab_log_${new Date().toISOString().replace(/[:.]/g, "-")}.log`;
        a.click();
        URL.revokeObjectURL(url);
      });
    }

    const fsBtn = this.containerEl.querySelector("#fullscreenBtn");
    if (fsBtn) {
      fsBtn.addEventListener("click", () => {
        this.isFullscreen = !this.isFullscreen;
        const root = this.containerEl.querySelector("#terminalRoot");
        if (root) {
          root.classList.toggle("fullscreen", this.isFullscreen);
        }
      });
    }

    this.renderBody();
  }

  renderBody() {
    const bodyEl = this.containerEl.querySelector("#terminalBody");
    const countBadge = this.containerEl.querySelector("#logCountBadge");
    if (countBadge) countBadge.textContent = `${this.logs.length} lines`;
    if (!bodyEl) return;

    if (this.logs.length === 0) {
      bodyEl.innerHTML = `<div class="log-empty">Waiting for real-time log stream from Colab runtime...</div>`;
      return;
    }

    let filtered = this.logs;

    // Filter by level
    if (this.levelFilter === "error") {
      filtered = filtered.filter((l) => {
        const lower = String(l).toLowerCase();
        return lower.includes("error") || lower.includes("fail") || lower.includes("exception") || lower.includes("traceback") || lower.includes("lỗi");
      });
    } else if (this.levelFilter === "success") {
      filtered = filtered.filter((l) => {
        const lower = String(l).toLowerCase();
        return lower.includes("success") || lower.includes("completed") || lower.includes("done") || lower.includes("hoàn thành") || lower.includes("[ok]");
      });
    }

    // Filter by search
    if (this.searchQuery) {
      filtered = filtered.filter((l) => String(l).toLowerCase().includes(this.searchQuery));
    }

    if (filtered.length === 0) {
      bodyEl.innerHTML = `<div class="log-empty">No logs matching current filter.</div>`;
      return;
    }

    const html = filtered.map(formatRichLogLine).join("");
    bodyEl.innerHTML = html;

    if (this.autoScroll) {
      this.scrollToBottom();
    }
  }

  scrollToBottom() {
    const bodyEl = this.containerEl.querySelector("#terminalBody");
    if (bodyEl) {
      bodyEl.scrollTop = bodyEl.scrollHeight;
    }
  }
}
