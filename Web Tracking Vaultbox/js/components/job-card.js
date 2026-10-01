import { escapeHtml, formatBytes, formatSpeed, formatDuration, formatStopwatch, getJobId } from "../formatters.js";
import { icons } from "../icons.js";

export class JobCardComponent {
  constructor(containerEl, onSelectJob = null) {
    this.containerEl = containerEl;
    this.onSelectJob = onSelectJob;
    this.jobs = [];
    this.selectedJobId = null;
    this.currentJob = null;
    this.queueExpanded = true;
  }

  setJobsData(jobsList, selectedJobId = null) {
    this.jobs = Array.isArray(jobsList) ? [...jobsList] : [];

    // Ensure every job has normalized jobId
    for (const j of this.jobs) {
      if (!j.jobId) j.jobId = getJobId(j);
    }

    // Stable sort jobs: by createdAt or id
    this.jobs.sort((a, b) => {
      const ta = a.createdAt || a.created_at || 0;
      const tb = b.createdAt || b.created_at || 0;
      return ta - tb || String(a.jobId).localeCompare(String(b.jobId));
    });

    // Determine selectedJobId stably
    if (selectedJobId && this.jobs.some((j) => String(j.jobId) === String(selectedJobId))) {
      this.selectedJobId = String(selectedJobId);
    } else if (this.selectedJobId && this.jobs.some((j) => String(j.jobId) === String(this.selectedJobId))) {
      // Keep existing user selection locked!
    } else if (this.jobs.length > 0) {
      // Pick first running job or first job and lock it
      const active = this.jobs.find((j) => ["running", "downloading", "uploading", "extracting", "optimizing"].includes(String(j.status || "").toLowerCase()));
      this.selectedJobId = String((active || this.jobs[0]).jobId);
    } else {
      this.selectedJobId = null;
    }

    this.currentJob = this.jobs.find((j) => String(j.jobId) === String(this.selectedJobId)) || (this.jobs.length > 0 ? this.jobs[0] : null);

    this.render();
  }

  setJob(job) {
    if (!job) {
      this.setJobsData([]);
      return;
    }
    const jid = getJobId(job);
    const existingIdx = this.jobs.findIndex((j) => String(j.jobId || j.id || j.job_id) === jid);
    if (existingIdx >= 0) {
      this.jobs[existingIdx] = { ...this.jobs[existingIdx], ...job, jobId: jid };
    } else {
      this.jobs.push({ ...job, jobId: jid });
    }
    if (!this.selectedJobId) {
      this.selectedJobId = jid;
    }
    this.setJobsData(this.jobs, this.selectedJobId);
  }

  render() {
    if (!this.jobs || this.jobs.length === 0) {
      this.containerEl.innerHTML = `
        <div class="colab-job-card-wrapper" style="text-align: center; padding: 32px 20px;">
          <div style="width: 44px; height: 44px; margin: 0 auto 10px; border-radius: 50%; background: var(--panel-2); display: flex; align-items: center; justify-content: center; color: var(--muted);">
            ${icons.activity("icon-lg")}
          </div>
          <div style="font-weight: 700; font-size: 14px; margin-bottom: 4px; color: var(--text);">No Active Jobs On This Server</div>
          <div style="font-size: 11.5px; color: var(--muted); max-width: 360px; margin: 0 auto; line-height: 1.5;">
            The server runtime is connected and idle. When a transfer or optimize job is dispatched, live progress and stats will appear here.
          </div>
        </div>
      `;
      return;
    }

    const job = this.currentJob || this.jobs[0];
    const isOptimizeJob = Boolean(
      (job.options && job.options.is_optimize_page) ||
      (job.payload && job.payload.options && job.payload.options.is_optimize_page) ||
      (job.mode === "optimize") ||
      (job.name && job.name.toLowerCase().includes("optimize")) ||
      (job.optimized_files && job.optimized_files.length > 0) ||
      (job.optimizedFiles && job.optimizedFiles.length > 0)
    );

    const status = String(job.status || "idle").toLowerCase();
    const step = String(job.step || job.message || "").toLowerCase();

    // Calculate percentage
    let pct = 0;
    if (status === "completed") {
      pct = 100;
    } else if (typeof job.progress === "number") {
      pct = Math.min(100, Math.round(job.progress <= 1 ? job.progress * 100 : job.progress));
    } else if (job.progress && typeof job.progress === "object") {
      const p = job.progress;
      if (p.upload > 0) pct = Math.round(p.upload);
      else if (p.optimize > 0) pct = Math.round(p.optimize);
      else if (p.extract > 0) pct = Math.round(p.extract);
      else if (p.download > 0) pct = Math.round(p.download);
    }

    // Stepper phase determination
    const isDownloadActive = status === "downloading" || step.includes("download");
    const isOptimizeActive = status === "optimizing" || step.includes("optim");
    const isExtractActive = status === "extracting" || step.includes("extract");
    const isUploadActive = status === "uploading" || step.includes("upload");
    const isDone = status === "completed";

    // Item lists & files
    const items = job.items || (job.payload && job.payload.items) || [];
    const completedItems = job.completed_items || job.completedItems || [];
    const failedItems = job.failed_items || job.failedItems || [];
    const itemTimings = job.item_timings || job.itemTimings || {};

    const completedSet = new Set(completedItems.map((i) => (typeof i === "string" ? i : i.name || i.path || i.id)));
    const failedSet = new Set(failedItems.map((i) => (typeof i === "string" ? i : i.name || i.path || i.id)));

    // Provider Info
    const srcProvider = (job.source_provider || (job.payload?.source?.provider) || "SOURCE").toUpperCase();
    const dstProvider = (job.target_provider || (job.payload?.target?.provider) || "TARGET").toUpperCase();

    // Metrics
    const bytesDone = job.bytesDone || job.bytes_done || job.downloaded || 0;
    const bytesTotal = job.bytesTotal || job.bytes_total || job.total || 0;
    const speed = job.speed || 0;
    const currentFile = job.currentFile || job.current_file || "-";

    // Build Tabs Bar
    const tabsHtml = this.jobs
      .map((j) => {
        const jid = String(j.jobId || j.id || j.job_id || "");
        const jActive = String(this.selectedJobId) === jid;
        const jOpt = Boolean((j.options && j.options.is_optimize_page) || (j.mode === "optimize") || (j.name && j.name.toLowerCase().includes("opt")));
        const jStatus = String(j.status || "idle").toLowerCase();

        let jPct = 0;
        if (jStatus === "completed") jPct = 100;
        else if (typeof j.progress === "number") jPct = Math.min(100, Math.round(j.progress <= 1 ? j.progress * 100 : j.progress));
        else if (j.progress && typeof j.progress === "object") {
          const jp = j.progress;
          jPct = Math.round(jp.upload || jp.optimize || jp.extract || jp.download || 0);
        }

        let dotClass = "waiting";
        if (["running", "downloading", "uploading", "extracting", "optimizing"].includes(jStatus)) dotClass = "running";
        else if (jStatus === "completed") dotClass = "done";
        else if (["failed", "error", "cancelled"].includes(jStatus)) dotClass = "error";

        const titleText = j.name || j.current_file || (jOpt ? "Optimize Job" : "Transfer Job");

        return `
          <button class="colab-job-tab-btn ${jOpt ? "type-opt" : "type-transfer"} ${jActive ? "active" : ""}" data-job-id="${escapeHtml(jid)}">
            <span class="colab-tab-type-icon ${jOpt ? "opt" : "transfer"}">
              ${jOpt ? icons.image("icon-sm") : icons.zap("icon-sm")}
            </span>
            <span class="colab-job-tab-dot ${dotClass}"></span>
            <span style="max-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
              ${escapeHtml(titleText)}
            </span>
            <span class="colab-job-tab-pct">${jPct}%</span>
          </button>
        `;
      })
      .join("");

    // Stepper HTML
    let stepperHtml = "";
    if (isOptimizeJob) {
      stepperHtml = `
        <div class="colab-stepper-container">
          <div class="colab-stepper-track">
            <div class="colab-stepper-line-bg"></div>
            <div class="colab-stepper-progress-line" style="width: ${isDone ? "100%" : isUploadActive ? "80%" : isOptimizeActive ? "50%" : "20%"};"></div>

            <div class="colab-step-item ${isDone || isOptimizeActive || isUploadActive ? "completed" : isDownloadActive ? "active" : ""}">
              <div class="colab-step-circle-wrap">
                <span class="colab-step-pulse-ring"></span>
                <div class="colab-step-circle">${isDone || isOptimizeActive || isUploadActive ? "✓" : "1"}</div>
              </div>
              <div class="colab-step-text">
                <span class="colab-step-title">Download</span>
              </div>
            </div>

            <div class="colab-step-item ${isDone || isUploadActive ? "completed" : isOptimizeActive ? "active" : ""}">
              <div class="colab-step-circle-wrap">
                <span class="colab-step-pulse-ring"></span>
                <div class="colab-step-circle">${isDone || isUploadActive ? "✓" : "2"}</div>
              </div>
              <div class="colab-step-text">
                <span class="colab-step-title">Optimize</span>
              </div>
            </div>

            <div class="colab-step-item ${isDone ? "completed" : isUploadActive ? "active" : ""}">
              <div class="colab-step-circle-wrap">
                <span class="colab-step-pulse-ring"></span>
                <div class="colab-step-circle">${isDone ? "✓" : "3"}</div>
              </div>
              <div class="colab-step-text">
                <span class="colab-step-title">Upload</span>
              </div>
            </div>
          </div>
        </div>
      `;
    } else {
      stepperHtml = `
        <div class="colab-stepper-container">
          <div class="colab-stepper-track">
            <div class="colab-stepper-line-bg"></div>
            <div class="colab-stepper-progress-line" style="width: ${isDone ? "100%" : isUploadActive ? "80%" : isExtractActive ? "50%" : "20%"};"></div>

            <div class="colab-step-item ${isDone || isExtractActive || isUploadActive ? "completed" : isDownloadActive ? "active" : ""}">
              <div class="colab-step-circle-wrap">
                <span class="colab-step-pulse-ring"></span>
                <div class="colab-step-circle">${isDone || isExtractActive || isUploadActive ? "✓" : "1"}</div>
              </div>
              <div class="colab-step-text">
                <span class="colab-step-title">Download</span>
              </div>
            </div>

            <div class="colab-step-item ${isDone || isUploadActive ? "completed" : isExtractActive ? "active" : ""}">
              <div class="colab-step-circle-wrap">
                <span class="colab-step-pulse-ring"></span>
                <div class="colab-step-circle">${isDone || isUploadActive ? "✓" : "2"}</div>
              </div>
              <div class="colab-step-text">
                <span class="colab-step-title">Process</span>
              </div>
            </div>

            <div class="colab-step-item ${isDone ? "completed" : isUploadActive ? "active" : ""}">
              <div class="colab-step-circle-wrap">
                <span class="colab-step-pulse-ring"></span>
                <div class="colab-step-circle">${isDone ? "✓" : "3"}</div>
              </div>
              <div class="colab-step-text">
                <span class="colab-step-title">Upload</span>
              </div>
            </div>
          </div>
        </div>
      `;
    }

    // Item Queue Rows
    let queueRowsHtml = "";
    if (items.length > 0) {
      queueRowsHtml = items
        .map((item, idx) => {
          const name = typeof item === "string" ? item : item.name || item.path || `File ${idx + 1}`;
          const key = typeof item === "string" ? item : item.id || item.path || item.name || String(idx);
          const timing = itemTimings[key] || itemTimings[name] || {};

          let badgeClass = "";
          let timeText = "--:--";

          if (completedSet.has(name) || completedSet.has(key) || timing.status === "done") {
            badgeClass = "done";
            timeText = timing.duration != null ? `${timing.duration}s` : "Done";
          } else if (failedSet.has(name) || failedSet.has(key) || timing.status === "skipped") {
            badgeClass = "skipped";
            timeText = "Skip";
          } else if (status !== "idle" && status !== "completed" && status !== "failed") {
            badgeClass = "active";
            const elapsed = timing.startTime ? Math.max(0, Math.floor((Date.now() / 1000) - timing.startTime)) : 0;
            timeText = formatStopwatch(elapsed);
          }

          return `
            <div class="queue-row">
              <span class="queue-row-name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
              <span class="time-pill ${escapeHtml(badgeClass)}">${escapeHtml(timeText)}</span>
            </div>
          `;
        })
        .join("");
    }

    // Optimization Comparison Table
    let comparisonHtml = "";
    const optFiles = job.optimized_files || job.optimizedFiles || [];
    if (optFiles.length > 0) {
      const rows = optFiles
        .map((f) => {
          const orig = f.originalSize || f.orig_size || 0;
          const opt = f.optimizedSize || f.opt_size || 0;
          const saved = orig > 0 ? Math.max(0, Math.round(((orig - opt) / orig) * 100)) : 0;
          return `
            <tr>
              <td title="${escapeHtml(f.name || "")}" style="max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(f.name || "")}</td>
              <td>${formatBytes(orig)}</td>
              <td>${formatBytes(opt)}</td>
              <td style="color: var(--ok); font-weight: 700;">-${saved}%</td>
            </tr>
          `;
        })
        .join("");

      comparisonHtml = `
        <div class="opt-table-wrap">
          <div style="font-size: 10.5px; font-weight: 700; color: var(--muted); text-transform: uppercase; margin-bottom: 6px; display: flex; align-items: center; gap: 6px;">
            ${icons.image("icon-sm")} Image Optimization Results (${optFiles.length} files)
          </div>
          <table class="opt-table">
            <thead>
              <tr>
                <th>File Name</th>
                <th>Original</th>
                <th>Optimized</th>
                <th>Saved</th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      `;
    }

    this.containerEl.innerHTML = `
      <div class="colab-jobs-bar-container">
        <div class="colab-jobs-tabs-scroll-wrapper">
          <button class="colab-tabs-scroll-btn" id="jobTabScrollLeft" title="Scroll left">
            ${icons.chevronLeft("icon-sm")}
          </button>
          <div class="colab-jobs-tabs-list" id="jobTabsList">
            ${tabsHtml}
          </div>
          <button class="colab-tabs-scroll-btn" id="jobTabScrollRight" title="Scroll right">
            ${icons.chevronRight("icon-sm")}
          </button>
        </div>
      </div>

      <div class="colab-job-card-wrapper">
        <div class="colab-card-header-unified">
          <div class="colab-card-title-group">
            <div class="colab-card-header-icon">
              ${isOptimizeJob ? icons.image("icon-md") : icons.package("icon-md")}
            </div>
            <div>
              <div class="colab-card-main-title">${escapeHtml(job.name || job.current_file || "Transfer Job")}</div>
              <div class="colab-card-subtitle">
                <span class="badge-code">${escapeHtml(srcProvider)}</span>
                ${icons.arrowRight("icon-sm")}
                <span class="badge-code">${escapeHtml(dstProvider)}</span>
                <span style="margin-left: 6px;">ID: <code style="color: var(--accent);">${escapeHtml(job.id || job.job_id || "-")}</code></span>
              </div>
            </div>
          </div>
          <span class="colab-status-badge ${escapeHtml(status)}">
            <span class="pulse-dot ${status === "completed" ? "online" : status === "idle" ? "offline" : "active"}"></span>
            ${escapeHtml(job.status || "idle")}
          </span>
        </div>

        ${stepperHtml}

        <div class="colab-progress-wrapper">
          <div class="colab-progress-header">
            <span>${escapeHtml(job.message || job.step || "Processing...")}</span>
            <span style="font-family: var(--font-mono); font-size: 12px; color: var(--src-color); font-weight: 700;">${pct}%</span>
          </div>
          <div class="colab-progress-bar-container">
            <div class="colab-progress-bar-fill" style="width: ${pct}%;"></div>
          </div>
        </div>

        <!-- 6-Column VaultBox Stats Grid -->
        <div class="colab-progress-stats-grid">
          <div class="colab-stat-card">
            <span class="colab-stat-label">Status</span>
            <span class="colab-stat-value status-${escapeHtml(status)}">${escapeHtml(status.toUpperCase())}</span>
          </div>
          <div class="colab-stat-card">
            <span class="colab-stat-label">Speed</span>
            <span class="colab-stat-value" style="color: var(--accent);">${formatSpeed(speed)}</span>
          </div>
          <div class="colab-stat-card">
            <span class="colab-stat-label">Transferred</span>
            <span class="colab-stat-value">${formatBytes(bytesDone)} / ${formatBytes(bytesTotal)}</span>
          </div>
          <div class="colab-stat-card">
            <span class="colab-stat-label">Items</span>
            <span class="colab-stat-value">${completedItems.length} / ${items.length > 0 ? items.length : (job.files_to_download || 1)}</span>
          </div>
          <div class="colab-stat-card" title="${escapeHtml(currentFile)}">
            <span class="colab-stat-label">Active File</span>
            <span class="colab-stat-value" style="color: #93c5fd;">${escapeHtml(currentFile)}</span>
          </div>
          <div class="colab-stat-card">
            <span class="colab-stat-label">Elapsed</span>
            <span class="colab-stat-value">${job.created_at ? formatDuration((Date.now() / 1000) - job.created_at) : "0s"}</span>
          </div>
        </div>

        ${items.length > 0 ? `
          <div class="queue-box">
            <div class="queue-header-bar" id="queueToggleBtn">
              <span style="display:flex; align-items:center; gap:6px;">
                ${icons.layers("icon-sm")} Items Queue (${completedItems.length}/${items.length} completed)
              </span>
              <span id="queueArrow" style="display:flex; align-items:center;">
                ${this.queueExpanded ? icons.chevronUp("icon-sm") : icons.chevronDown("icon-sm")}
              </span>
            </div>
            <div class="queue-items-container" id="queueListBody" style="${this.queueExpanded ? "" : "display:none;"}">
              ${queueRowsHtml}
            </div>
          </div>
        ` : ""}

        ${comparisonHtml}
      </div>
    `;

    // Bind tab clicks
    this.containerEl.querySelectorAll(".colab-job-tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const jid = btn.dataset.jobId;
        if (jid) {
          this.selectedJobId = jid;
          if (this.onSelectJob) {
            this.onSelectJob(jid);
          } else {
            this.setJobsData(this.jobs, jid);
          }
        }
      });
    });

    // Bind horizontal scroll buttons
    const scrollLeft = this.containerEl.querySelector("#jobTabScrollLeft");
    const scrollRight = this.containerEl.querySelector("#jobTabScrollRight");
    const tabsList = this.containerEl.querySelector("#jobTabsList");
    if (scrollLeft && tabsList) {
      scrollLeft.addEventListener("click", () => {
        tabsList.scrollBy({ left: -150, behavior: "smooth" });
      });
    }
    if (scrollRight && tabsList) {
      scrollRight.addEventListener("click", () => {
        tabsList.scrollBy({ left: 150, behavior: "smooth" });
      });
    }

    // Bind queue accordion
    const toggleBtn = this.containerEl.querySelector("#queueToggleBtn");
    if (toggleBtn) {
      toggleBtn.addEventListener("click", () => {
        this.queueExpanded = !this.queueExpanded;
        const body = this.containerEl.querySelector("#queueListBody");
        const arrow = this.containerEl.querySelector("#queueArrow");
        if (body) body.style.display = this.queueExpanded ? "flex" : "none";
        if (arrow) arrow.innerHTML = this.queueExpanded ? icons.chevronUp("icon-sm") : icons.chevronDown("icon-sm");
      });
    }
  }
}
