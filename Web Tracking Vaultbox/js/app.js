import { api } from "./api.js";
import { escapeHtml, formatRelativeTime, formatExpiresIn, getJobId } from "./formatters.js";
import { icons } from "./icons.js";
import { ServerListComponent } from "./components/server-list.js";
import { JobCardComponent } from "./components/job-card.js";
import { LogTerminalComponent } from "./components/log-terminal.js";

class App {
  constructor() {
    this.servers = [];
    this.selectedRoomId = null;
    this.selectedServer = null;
    this.jobsMap = new Map(); // jobId -> job object
    this.selectedJobId = null;
    this.activeMobileTab = "servers";

    // DOM Elements
    this.totalServersEl = document.getElementById("totalServersStat");
    this.onlineServersEl = document.getElementById("onlineServersStat");
    this.activeJobsEl = document.getElementById("activeJobsStat");
    this.wsStatusEl = document.getElementById("wsStatusDot");
    this.wsStatusTextEl = document.getElementById("wsStatusText");
    this.lastSyncEl = document.getElementById("lastSyncTime");

    this.serverBannerEl = document.getElementById("serverBannerContainer");
    this.jobContainerEl = document.getElementById("jobContainer");
    this.terminalContainerEl = document.getElementById("terminalContainer");
    this.serverListContainerEl = document.getElementById("serversList");
    this.sidebarEl = document.getElementById("sidebarSection");
    this.mainContentEl = document.getElementById("mainContentSection");
    this.mobServerCountEl = document.getElementById("mobServerCount");

    // Initialize Components
    this.serverList = new ServerListComponent(
      this.serverListContainerEl,
      (roomId) => this.selectServer(roomId),
      (server) => this.handleDeleteServer(server)
    );
    this.jobCard = new JobCardComponent(this.jobContainerEl, (jobId) => this.selectJob(jobId));
    this.terminal = new LogTerminalComponent(this.terminalContainerEl);

    this.init();
  }

  async init() {
    this.injectStaticIcons();
    this.terminal.render();
    this.jobCard.render();
    this.bindHeaderEvents();
    this.bindSearchAndFilters();
    this.bindMobileNavigation();
    this.applyMobileTabVisibility();

    // 1. Fetch initial snapshot
    await this.fetchInitialData();

    // 2. Connect persistent WebSocket streaming for Registry (Zero-polling)
    this.connectRegistryStream();
  }

  injectStaticIcons() {
    const brandLogo = document.getElementById("brandLogo");
    if (brandLogo) brandLogo.innerHTML = icons.zap("icon-md");

    const clockWrap = document.getElementById("clockIconWrap");
    if (clockWrap) clockWrap.innerHTML = icons.clock("icon-sm");

    const refreshWrap = document.getElementById("refreshIconWrap");
    if (refreshWrap) refreshWrap.innerHTML = icons.refresh("icon-sm");

    const trashWrap = document.getElementById("trashIconWrap");
    if (trashWrap) trashWrap.innerHTML = icons.trash("icon-sm");

    const settingsWrap = document.getElementById("settingsIconWrap");
    if (settingsWrap) settingsWrap.innerHTML = icons.settings("icon-sm");

    const searchWrap = document.getElementById("searchIconWrap");
    if (searchWrap) searchWrap.innerHTML = icons.search("icon-sm");

    const mobServerIcon = document.getElementById("mobServerIcon");
    if (mobServerIcon) mobServerIcon.innerHTML = icons.server("icon-sm");

    const mobJobIcon = document.getElementById("mobJobIcon");
    if (mobJobIcon) mobJobIcon.innerHTML = icons.zap("icon-sm");

    const mobTermIcon = document.getElementById("mobTermIcon");
    if (mobTermIcon) mobTermIcon.innerHTML = icons.terminal("icon-sm");

    const loadingWrap = document.getElementById("loadingIconWrap");
    if (loadingWrap) loadingWrap.innerHTML = icons.refresh("icon-md");
  }

  bindHeaderEvents() {
    const refreshBtn = document.getElementById("refreshBtn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", async () => {
        const refreshIcon = refreshBtn.querySelector("#refreshIconWrap svg");
        if (refreshIcon) refreshIcon.style.animation = "breathing 0.8s linear infinite";
        await this.fetchInitialData();
        if (this.selectedRoomId) {
          await this.loadSelectedServerDetail();
        }
        if (refreshIcon) refreshIcon.style.animation = "";
      });
    }

    const clearJobsBtn = document.getElementById("clearJobsBtn");
    if (clearJobsBtn) {
      clearJobsBtn.addEventListener("click", async () => {
        if (confirm("Are you sure you want to clear all jobs history across all servers?")) {
          try {
            await api.purgeAllJobs();
            this.jobsMap.clear();
            this.selectedJobId = null;
            this.jobCard.setJobsData([]);
            this.terminal.setLogs([]);
            await this.fetchInitialData();
            if (this.selectedRoomId) {
              await this.loadSelectedServerDetail();
            }
          } catch (err) {
            alert(`Failed to clear jobs: ${err.message}`);
          }
        }
      });
    }

    const relayConfigBtn = document.getElementById("relayConfigBtn");
    if (relayConfigBtn) {
      relayConfigBtn.addEventListener("click", () => {
        const current = api.getRelayUrl();
        const input = prompt("Enter Cloudflare Relay Server URL:", current);
        if (input && input.trim() && input !== current) {
          api.setRelayUrl(input.trim());
          this.fetchInitialData();
        }
      });
    }
  }

  bindSearchAndFilters() {
    const searchInput = document.getElementById("serverSearchInput");
    if (searchInput) {
      searchInput.addEventListener("input", (e) => {
        this.serverList.setSearchQuery(e.target.value);
      });
    }

    const filterPills = document.querySelectorAll(".filter-pill");
    filterPills.forEach((pill) => {
      pill.addEventListener("click", () => {
        filterPills.forEach((p) => p.classList.remove("active"));
        pill.classList.add("active");
        const filter = pill.dataset.filter || "all";
        this.serverList.setFilter(filter);
      });
    });
  }

  bindMobileNavigation() {
    const tabBtns = document.querySelectorAll(".mobile-tab-btn");
    tabBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        tabBtns.forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const tab = btn.dataset.tab;
        this.activeMobileTab = tab;
        this.applyMobileTabVisibility();
      });
    });

    window.addEventListener("resize", () => {
      this.applyMobileTabVisibility();
    });
  }

  applyMobileTabVisibility() {
    const isMobile = window.innerWidth <= 768;

    if (!isMobile) {
      if (this.sidebarEl) this.sidebarEl.classList.remove("mobile-visible");
      if (this.mainContentEl) this.mainContentEl.classList.remove("mobile-hidden");
      if (this.serverBannerEl) this.serverBannerEl.style.display = "";
      if (this.jobContainerEl) this.jobContainerEl.style.display = "";
      if (this.terminalContainerEl) this.terminalContainerEl.style.display = "";
      return;
    }

    if (this.activeMobileTab === "servers") {
      if (this.sidebarEl) this.sidebarEl.classList.add("mobile-visible");
      if (this.mainContentEl) this.mainContentEl.classList.add("mobile-hidden");
    } else if (this.activeMobileTab === "job") {
      if (this.sidebarEl) this.sidebarEl.classList.remove("mobile-visible");
      if (this.mainContentEl) this.mainContentEl.classList.remove("mobile-hidden");
      if (this.serverBannerEl) this.serverBannerEl.style.display = "flex";
      if (this.jobContainerEl) this.jobContainerEl.style.display = "flex";
      if (this.terminalContainerEl) this.terminalContainerEl.style.display = "none";
    } else if (this.activeMobileTab === "terminal") {
      if (this.sidebarEl) this.sidebarEl.classList.remove("mobile-visible");
      if (this.mainContentEl) this.mainContentEl.classList.remove("mobile-hidden");
      if (this.serverBannerEl) this.serverBannerEl.style.display = "none";
      if (this.jobContainerEl) this.jobContainerEl.style.display = "none";
      if (this.terminalContainerEl) this.terminalContainerEl.style.display = "block";
    }
  }

  async fetchInitialData() {
    try {
      const data = await api.fetchServers();
      this.handleServersUpdate(data);
    } catch (err) {
      console.warn("[Tracking] Error fetching initial servers:", err);
    }
  }

  connectRegistryStream() {
    api.connectRegistryWebSocket(
      (data) => this.handleServersUpdate(data),
      (state) => this.handleRegistryState(state)
    );
  }

  handleServersUpdate(data) {
    if (!data) return;
    const rawList = Array.isArray(data.servers) ? data.servers : [];
    this.servers = rawList.slice(0, 10);

    // Update Header Stats
    if (this.totalServersEl) this.totalServersEl.textContent = this.servers.length;
    if (this.onlineServersEl) this.onlineServersEl.textContent = data.onlineCount || this.servers.filter((s) => s.online).length;
    if (this.mobServerCountEl) this.mobServerCountEl.textContent = this.servers.length;

    const totalActiveJobs = this.servers.reduce((sum, s) => sum + (s.activeJobs || 0), 0);
    if (this.activeJobsEl) this.activeJobsEl.textContent = totalActiveJobs;
    if (this.lastSyncEl) this.lastSyncEl.textContent = new Date().toLocaleTimeString();

    // Auto-select first online server or first server if none selected
    if (!this.selectedRoomId && this.servers.length > 0) {
      const firstOnline = this.servers.find((s) => s.online);
      this.selectServer(firstOnline ? firstOnline.roomId : this.servers[0].roomId);
    } else if (this.selectedRoomId) {
      const current = this.servers.find((s) => s.roomId === this.selectedRoomId);
      if (current) {
        this.serverList.setServers(this.servers, this.selectedRoomId);
        if (this.selectedServer) {
          this.selectedServer = { ...this.selectedServer, ...current };
          this.renderServerBanner(this.selectedServer);
        }
      } else if (this.servers.length > 0) {
        // Previously selected server was deleted, select first available
        this.selectServer(this.servers[0].roomId);
      } else {
        this.selectedRoomId = null;
        this.selectedServer = null;
        this.jobsMap.clear();
        this.jobCard.setJobsData([]);
        this.terminal.setLogs([]);
        this.serverList.setServers([]);
        this.renderEmptyServerState();
      }
    } else {
      this.serverList.setServers(this.servers);
      this.renderEmptyServerState();
    }
  }

  async selectServer(roomId) {
    if (!roomId) return;
    if (this.selectedRoomId === roomId && this.jobsMap.size > 0) {
      this.serverList.setActiveRoom(roomId);
      return;
    }
    this.selectedRoomId = roomId;
    this.serverList.setActiveRoom(roomId);
    this.jobsMap.clear();
    this.selectedJobId = null;

    await this.loadSelectedServerDetail();
    this.connectRoomStream(roomId);

    // On mobile, if a server is tapped, switch view to active job
    if (window.innerWidth <= 768) {
      const jobTabBtn = document.getElementById("mobTabJob");
      if (jobTabBtn) jobTabBtn.click();
    }
  }

  async loadSelectedServerDetail() {
    if (!this.selectedRoomId) return;
    try {
      const data = await api.fetchServerDetail(this.selectedRoomId);
      this.selectedServer = data;
      this.renderServerBanner(data);

      const jobs = data.jobs || [];
      for (const j of jobs) {
        const jid = getJobId(j);
        if (jid) this.jobsMap.set(jid, { ...j, jobId: jid });
      }
      if (data.snapshot?.job) {
        const snap = data.snapshot.job;
        const jid = getJobId(snap) || String(data.snapshot.jobId || "");
        if (jid) this.jobsMap.set(jid, { ...snap, jobId: jid });
      }

      const jobsList = Array.from(this.jobsMap.values());
      if (!this.selectedJobId && jobsList.length > 0) {
        const active = jobsList.find((j) => ["running", "downloading", "uploading", "extracting", "optimizing"].includes(String(j.status || "").toLowerCase()));
        this.selectedJobId = getJobId(active || jobsList[0]);
      }

      this.jobCard.setJobsData(jobsList, this.selectedJobId);

      const activeJob = (this.selectedJobId ? this.jobsMap.get(this.selectedJobId) : null) || (jobsList.length > 0 ? jobsList[0] : null);
      const logs = activeJob?.logs || data.logs || data.snapshot?.logs || [];
      this.terminal.setLogs(logs);
    } catch (err) {
      console.warn("[Tracking] Error loading server detail:", err);
    }
  }

  selectJob(jobId) {
    if (!jobId) return;
    this.selectedJobId = String(jobId);
    const jobsList = Array.from(this.jobsMap.values());
    this.jobCard.setJobsData(jobsList, this.selectedJobId);

    const selected = this.jobsMap.get(this.selectedJobId);
    if (selected && Array.isArray(selected.logs)) {
      this.terminal.setLogs(selected.logs);
    }
  }

  connectRoomStream(roomId) {
    api.connectRoomWebSocket(
      roomId,
      (msg) => this.handleRoomMessage(msg),
      (state) => this.handleRoomState(state)
    );
  }

  handleRoomMessage(msg) {
    if (!msg || typeof msg !== "object") return;

    if (msg.type === "ready") {
      if (this.selectedServer) {
        this.selectedServer.online = Boolean(msg.colabReady);
        this.renderServerBanner(this.selectedServer);
      }
    }

    if (msg.type === "progress" || msg.type === "snapshot" || msg.type === "done" || msg.type === "error") {
      const job = msg.job || msg;
      const jid = getJobId(job) || String(msg.jobId || "");
      if (jid && job) {
        const existing = this.jobsMap.get(jid) || {};
        const merged = { ...existing, ...job, jobId: jid };
        this.jobsMap.set(jid, merged);

        const jobsList = Array.from(this.jobsMap.values());

        // If no job was selected yet, lock to this first one
        if (!this.selectedJobId) {
          this.selectedJobId = jid;
        }

        // Pass locked selectedJobId to jobCard
        this.jobCard.setJobsData(jobsList, this.selectedJobId);

        // ONLY update live terminal logs if this incoming update is for the CURRENTLY selected job
        if (this.selectedJobId === jid) {
          if (merged.logs && Array.isArray(merged.logs)) {
            this.terminal.setLogs(merged.logs);
          }
        }
      }
    }
  }

  handleRegistryState(state) {
    if (!this.wsStatusEl || !this.wsStatusTextEl) return;
    if (state === "connected") {
      this.wsStatusEl.className = "pulse-dot online";
      this.wsStatusTextEl.textContent = "Live WS Connected";
    } else if (state === "connecting") {
      this.wsStatusEl.className = "pulse-dot active";
      this.wsStatusTextEl.textContent = "Connecting WS...";
    } else {
      this.wsStatusEl.className = "pulse-dot offline";
      this.wsStatusTextEl.textContent = "WS Reconnecting...";
    }
  }

  handleRoomState(state) {
    // Room streaming state updates can update badges if needed
  }

  async handleDeleteServer(server) {
    if (!server || !server.roomId) return;
    const name = server.name || server.roomId;
    if (!confirm(`Remove server "${name}" from tracking dashboard?`)) return;

    try {
      const roomId = server.roomId;
      const wasSelected = this.selectedRoomId === roomId;

      if (wasSelected) {
        api.disconnectRoomWebSocket();
      }

      await api.deleteServer(roomId);

      this.servers = this.servers.filter((s) => s.roomId !== roomId);

      if (wasSelected) {
        this.selectedRoomId = null;
        this.selectedServer = null;
        this.jobsMap.clear();
        this.jobCard.setJobsData([]);
        this.terminal.setLogs([]);
      }

      this.handleServersUpdate({
        servers: this.servers,
        onlineCount: this.servers.filter((s) => s.online).length,
      });
    } catch (err) {
      alert(`Failed to remove server: ${err.message}`);
    }
  }

  renderServerBanner(server) {
    if (!this.serverBannerEl || !server) return;
    const isOnline = Boolean(server.online || server.colabReady);
    const env = (server.environment || "Colab").toLowerCase();
    const lastActiveText = formatRelativeTime(server.lastActive);
    const expiresInText = !isOnline && server.expiresInMs ? formatExpiresIn(server.expiresInMs) : "";

    this.serverBannerEl.innerHTML = `
      <div class="server-hero-banner">
        <div class="hero-left">
          <div class="hero-status-icon-wrapper ${isOnline ? "online" : "offline"}">
            ${isOnline ? icons.server("icon-md") : icons.wifiOff("icon-md")}
          </div>
          <div class="hero-title-area">
            <div class="hero-title-row">
              <span class="hero-server-name">${escapeHtml(server.serverName || server.name || server.roomId)}</span>
              <span class="env-tag ${escapeHtml(env)}">${escapeHtml(server.environment || "Colab")}</span>
              <span class="stat-chip" style="font-size: 10.5px; padding: 2px 8px; ${isOnline ? "color:var(--ok); border-color:rgba(16,185,129,0.35);" : "color:var(--muted);"}">
                <span class="pulse-dot ${isOnline ? "online" : "offline"}"></span>
                ${isOnline ? "ONLINE" : "OFFLINE"}
              </span>
            </div>
            <div class="hero-details-row">
              <span>Room: <code class="badge-code">${escapeHtml(server.roomId || "-")}</code></span>
              <span>Last Active: <b style="color:var(--text);">${lastActiveText}</b></span>
              ${expiresInText ? `<span style="color:var(--warning); font-weight:600;">(${escapeHtml(expiresInText)})</span>` : ""}
              <span>Active Sockets: <b>${server.sockets || 1}</b></span>
            </div>
          </div>
        </div>

        <div style="display: flex; align-items: center; gap: 6px;">
          <button class="btn btn-sm" id="copyRoomBtn">
            ${icons.copy("icon-sm")}
            <span>Copy Room ID</span>
          </button>
          <button class="btn btn-sm" id="deleteServerBtn" style="color: var(--danger); border-color: rgba(248, 113, 113, 0.25);" title="Remove server from tracking">
            ${icons.trash("icon-sm")}
            <span>Remove</span>
          </button>
        </div>
      </div>
    `;

    const copyRoomBtn = this.serverBannerEl.querySelector("#copyRoomBtn");
    if (copyRoomBtn) {
      copyRoomBtn.addEventListener("click", () => {
        navigator.clipboard.writeText(server.roomId || "");
        copyRoomBtn.innerHTML = `${icons.checkCircle("icon-sm")} <span>Copied ✓</span>`;
        setTimeout(() => {
          copyRoomBtn.innerHTML = `${icons.copy("icon-sm")} <span>Copy Room ID</span>`;
        }, 2000);
      });
    }

    const deleteServerBtn = this.serverBannerEl.querySelector("#deleteServerBtn");
    if (deleteServerBtn) {
      deleteServerBtn.addEventListener("click", () => {
        this.handleDeleteServer(server);
      });
    }
  }

  renderEmptyServerState() {
    if (this.serverBannerEl) {
      this.serverBannerEl.innerHTML = `
        <div class="server-hero-banner" style="justify-content: center; text-align: center; padding: 30px 20px;">
          <div style="display: flex; flex-direction: column; align-items: center; gap: 8px;">
            <div class="empty-state-icon">
              ${icons.wifi("icon-lg")}
            </div>
            <div style="font-weight: 700; font-size: 14px; color: var(--text);">No Colab Servers Connected Yet</div>
            <div style="font-size: 11.5px; color: var(--muted); max-width: 420px; line-height: 1.5;">
              When you run the Colab / Kaggle launcher notebook or local worker, it will automatically register and stream live logs to this dashboard.
            </div>
          </div>
        </div>
      `;
    }
  }
}

// Start application when DOM is ready
window.addEventListener("DOMContentLoaded", () => {
  new App();
});
