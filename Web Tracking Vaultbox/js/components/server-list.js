import { escapeHtml, formatRelativeTime, formatExpiresIn } from "../formatters.js";
import { icons } from "../icons.js";

export class ServerListComponent {
  constructor(containerEl, onSelectServer, onDeleteServer = null) {
    this.containerEl = containerEl;
    this.onSelectServer = onSelectServer;
    this.onDeleteServer = onDeleteServer;
    this.servers = [];
    this.activeRoomId = null;
    this.searchQuery = "";
    this.currentFilter = "all"; // all, online, offline, active
  }

  setServers(servers, activeRoomId = null) {
    this.servers = Array.isArray(servers) ? servers.slice(0, 10) : [];
    if (activeRoomId) this.activeRoomId = activeRoomId;
    this.render();
  }

  setActiveRoom(roomId) {
    this.activeRoomId = roomId;
    this.render();
  }

  setSearchQuery(query) {
    this.searchQuery = (query || "").trim().toLowerCase();
    this.render();
  }

  setFilter(filter) {
    this.currentFilter = filter;
    this.render();
  }

  getFilteredServers() {
    const list = this.servers.filter((s) => {
      // Status filter
      if (this.currentFilter === "online" && !s.online) return false;
      if (this.currentFilter === "offline" && s.online) return false;
      if (this.currentFilter === "active" && (!s.activeJobs || s.activeJobs <= 0)) return false;

      // Search filter
      if (this.searchQuery) {
        const name = (s.name || "").toLowerCase();
        const room = (s.roomId || "").toLowerCase();
        const env = (s.environment || "").toLowerCase();
        return name.includes(this.searchQuery) || room.includes(this.searchQuery) || env.includes(this.searchQuery);
      }
      return true;
    });

    // 100% Stable sort: online first, then by firstSeen (creation order) so cards NEVER jump
    list.sort((a, b) => {
      if (a.online && !b.online) return -1;
      if (!a.online && b.online) return 1;
      return (a.firstSeen || 0) - (b.firstSeen || 0) || String(a.roomId).localeCompare(String(b.roomId));
    });

    return list.slice(0, 10);
  }

  render() {
    const filtered = this.getFilteredServers();

    if (filtered.length === 0) {
      this.containerEl.innerHTML = `
        <div class="empty-state-box">
          <div class="empty-state-icon">
            ${icons.server("icon-lg")}
          </div>
          <div style="font-weight: 700; font-size: 13px; margin-bottom: 4px; color: var(--text);">
            ${this.servers.length === 0 ? "No Active Servers" : "No Matching Servers"}
          </div>
          <div style="font-size: 11px; color: var(--muted); line-height: 1.4;">
            ${this.servers.length === 0 ? "Start a Colab or Kaggle server launcher to begin tracking (max 10 servers)." : "Try changing your search query or filter."}
          </div>
        </div>
      `;
      return;
    }

    this.containerEl.innerHTML = filtered
      .map((server) => {
        const isSelected = this.activeRoomId === server.roomId;
        const env = (server.environment || "Colab").toLowerCase();
        const isOnline = Boolean(server.online);
        const lastActiveText = formatRelativeTime(server.lastActive);
        const expiresInText = !isOnline && server.expiresInMs ? formatExpiresIn(server.expiresInMs) : "";

        const hasActiveJobs = (server.activeJobs || 0) > 0;
        const jobCountBadge = hasActiveJobs
          ? `<span class="stat-chip" style="padding: 1px 6px; font-size: 9.5px; background: rgba(56,189,248,0.15); color: var(--src-color); border-color: rgba(56,189,248,0.35);">
              ${icons.zap("icon-sm")} ${server.activeJobs} active
            </span>`
          : "";

        return `
          <div class="server-card ${isSelected ? "active" : ""}" data-room-id="${escapeHtml(server.roomId)}">
            <div class="server-card-header">
              <div class="server-name-group">
                <span class="pulse-dot ${isOnline ? "online" : "offline"}"></span>
                <span class="server-name" title="${escapeHtml(server.name || server.roomId)}">${escapeHtml(server.name || server.roomId)}</span>
              </div>
              <div style="display: flex; align-items: center; gap: 6px;">
                <span class="env-tag ${escapeHtml(env)}">${escapeHtml(server.environment || "Colab")}</span>
                <button class="server-card-delete-btn" data-delete-room="${escapeHtml(server.roomId)}" title="Remove server from tracking">
                  ${icons.trash("icon-sm")}
                </button>
              </div>
            </div>

            <div class="server-meta-row">
              <span class="room-id-tag" title="Room ID: ${escapeHtml(server.roomId)}">${escapeHtml(server.roomId)}</span>
              ${jobCountBadge}
            </div>

            <div class="server-meta-row" style="margin-top: 6px;">
              <span class="server-status-text ${isOnline ? "online" : "offline"}">
                ${isOnline ? "Online" : `Off: ${lastActiveText}`}
              </span>
              ${expiresInText ? `<span class="server-expiry-text ${server.expiresInMs < 3600000 ? "urgent" : ""}" title="Server will be automatically removed 12 hours after disconnecting">${escapeHtml(expiresInText)}</span>` : ""}
            </div>
          </div>
        `;
      })
      .join("");

    // Attach click listeners for selecting server
    this.containerEl.querySelectorAll(".server-card").forEach((el) => {
      el.addEventListener("click", (e) => {
        if (e.target.closest(".server-card-delete-btn")) return;
        const roomId = el.dataset.roomId;
        if (roomId && this.onSelectServer) {
          this.onSelectServer(roomId);
        }
      });
    });

    // Attach click listeners for card delete button
    this.containerEl.querySelectorAll(".server-card-delete-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const roomId = btn.dataset.deleteRoom;
        if (roomId && this.onDeleteServer) {
          const s = this.servers.find((srv) => srv.roomId === roomId);
          this.onDeleteServer(s || { roomId });
        }
      });
    });
  }
}
