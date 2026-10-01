/**
 * VaultBox Cloudflare Relay API Client with Full WebSocket Streaming
 */

const DEFAULT_RELAY_URL = "https://vaultbox-colab-relay.lefantasusdelimei.workers.dev";
const STORAGE_KEY_RELAY_URL = "vb_tracking_relay_url";

export class RelayApiClient {
  constructor() {
    this.relayUrl = localStorage.getItem(STORAGE_KEY_RELAY_URL) || DEFAULT_RELAY_URL;
    
    // Registry WebSocket connection (for server list push updates)
    this.registryWs = null;
    this.registryReconnectTimer = null;
    this.onRegistryUpdateCallback = null;
    this.onRegistryStateCallback = null;

    // Room WebSocket connection (for selected server job & log push streaming)
    this.roomWs = null;
    this.activeRoomId = null;
    this.roomReconnectTimer = null;
    this.onRoomMessageCallback = null;
    this.onRoomStateCallback = null;
  }

  getRelayUrl() {
    return this.relayUrl;
  }

  setRelayUrl(url) {
    let clean = (url || "").trim().replace(/\/+$/, "");
    if (!clean) clean = DEFAULT_RELAY_URL;
    this.relayUrl = clean;
    localStorage.setItem(STORAGE_KEY_RELAY_URL, clean);
    this.reconnectAll();
  }

  getWsBaseUrl() {
    return this.relayUrl.replace(/^http:/, "ws:").replace(/^https:/, "wss:");
  }

  reconnectAll() {
    if (this.onRegistryUpdateCallback) {
      this.connectRegistryWebSocket(this.onRegistryUpdateCallback, this.onRegistryStateCallback);
    }
    if (this.activeRoomId && this.onRoomMessageCallback) {
      this.connectRoomWebSocket(this.activeRoomId, this.onRoomMessageCallback, this.onRoomStateCallback);
    }
  }

  // ==========================================
  // REGISTRY WEBSOCKET (ZERO-POLLING SERVER LIST)
  // ==========================================
  connectRegistryWebSocket(onUpdate, onStateChange) {
    this.disconnectRegistryWebSocket();
    this.onRegistryUpdateCallback = onUpdate;
    this.onRegistryStateCallback = onStateChange;

    const wsUrl = `${this.getWsBaseUrl()}/api/colab-relay/ws?room=__registry__&role=app`;

    try {
      if (this.onRegistryStateCallback) this.onRegistryStateCallback("connecting");
      this.registryWs = new WebSocket(wsUrl);

      this.registryWs.onopen = () => {
        if (this.onRegistryStateCallback) this.onRegistryStateCallback("connected");
      };

      this.registryWs.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === "servers_update" && this.onRegistryUpdateCallback) {
            this.onRegistryUpdateCallback(data);
          }
        } catch {
          // Ignore invalid JSON
        }
      };

      this.registryWs.onclose = () => {
        if (this.onRegistryStateCallback) this.onRegistryStateCallback("disconnected");
        this.scheduleRegistryReconnect();
      };

      this.registryWs.onerror = () => {
        if (this.onRegistryStateCallback) this.onRegistryStateCallback("error");
      };
    } catch {
      if (this.onRegistryStateCallback) this.onRegistryStateCallback("error");
      this.scheduleRegistryReconnect();
    }
  }

  scheduleRegistryReconnect() {
    if (this.registryReconnectTimer) clearTimeout(this.registryReconnectTimer);
    this.registryReconnectTimer = setTimeout(() => {
      if (this.onRegistryUpdateCallback) {
        this.connectRegistryWebSocket(this.onRegistryUpdateCallback, this.onRegistryStateCallback);
      }
    }, 4000);
  }

  disconnectRegistryWebSocket() {
    if (this.registryReconnectTimer) {
      clearTimeout(this.registryReconnectTimer);
      this.registryReconnectTimer = null;
    }
    if (this.registryWs) {
      try { this.registryWs.close(); } catch {}
      this.registryWs = null;
    }
  }

  // ==========================================
  // ROOM WEBSOCKET (ZERO-POLLING JOB & LOG STREAMING)
  // ==========================================
  connectRoomWebSocket(roomId, onMessage, onStateChange) {
    this.disconnectRoomWebSocket();
    if (!roomId) return;

    this.activeRoomId = roomId;
    this.onRoomMessageCallback = onMessage;
    this.onRoomStateCallback = onStateChange;

    const wsUrl = `${this.getWsBaseUrl()}/api/colab-relay/ws?room=${encodeURIComponent(roomId)}&role=app`;

    try {
      if (this.onRoomStateCallback) this.onRoomStateCallback("connecting");
      this.roomWs = new WebSocket(wsUrl);

      this.roomWs.onopen = () => {
        if (this.onRoomStateCallback) this.onRoomStateCallback("connected");
      };

      this.roomWs.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (this.onRoomMessageCallback) {
            this.onRoomMessageCallback(data);
          }
        } catch {
          // Ignore invalid JSON
        }
      };

      this.roomWs.onclose = (event) => {
        if (this.onRoomStateCallback) this.onRoomStateCallback("disconnected");
        if (event && (event.reason === "purged" || event.code === 1000)) {
          return;
        }
        this.scheduleRoomReconnect(roomId);
      };

      this.roomWs.onerror = () => {
        if (this.onRoomStateCallback) this.onRoomStateCallback("error");
      };
    } catch {
      if (this.onRoomStateCallback) this.onRoomStateCallback("error");
      this.scheduleRoomReconnect(roomId);
    }
  }

  scheduleRoomReconnect(roomId) {
    if (this.roomReconnectTimer) clearTimeout(this.roomReconnectTimer);
    if (this.activeRoomId !== roomId) return;
    this.roomReconnectTimer = setTimeout(() => {
      if (this.activeRoomId === roomId) {
        this.connectRoomWebSocket(roomId, this.onRoomMessageCallback, this.onRoomStateCallback);
      }
    }, 4000);
  }

  disconnectRoomWebSocket() {
    if (this.roomReconnectTimer) {
      clearTimeout(this.roomReconnectTimer);
      this.roomReconnectTimer = null;
    }
    if (this.roomWs) {
      try { this.roomWs.close(); } catch {}
      this.roomWs = null;
    }
    this.activeRoomId = null;
  }

  // ==========================================
  // INITIAL / ON-DEMAND REST FALLBACKS & PURGE
  // ==========================================
  async fetchServers() {
    const url = `${this.relayUrl}/api/colab-relay/servers`;
    const res = await fetch(url, {
      method: "GET",
      headers: { "Accept": "application/json" },
      cache: "no-store",
    });
    if (!res.ok) {
      throw new Error(`Failed to fetch servers: HTTP ${res.status}`);
    }
    return await res.json();
  }

  async fetchServerDetail(roomId) {
    if (!roomId) throw new Error("Room ID is required");
    const cleanRoom = encodeURIComponent(roomId.trim());
    const url = `${this.relayUrl}/api/colab-relay/servers/${cleanRoom}`;
    const res = await fetch(url, {
      method: "GET",
      headers: { "Accept": "application/json" },
      cache: "no-store",
    });
    if (!res.ok) {
      const healthUrl = `${this.relayUrl}/api/colab-relay/health?room=${cleanRoom}`;
      const healthRes = await fetch(healthUrl, { cache: "no-store" });
      if (!healthRes.ok) throw new Error(`Server ${roomId} not reachable`);
      return await healthRes.json();
    }
    return await res.json();
  }

  async deleteServer(roomId) {
    if (!roomId) throw new Error("Room ID is required");
    const cleanRoom = encodeURIComponent(roomId.trim());
    const url = `${this.relayUrl}/api/colab-relay/servers/${cleanRoom}`;
    const res = await fetch(url, {
      method: "DELETE",
      headers: { "Accept": "application/json" },
    });
    if (!res.ok) {
      throw new Error(`Failed to delete server: HTTP ${res.status}`);
    }
    return await res.json();
  }

  async purgeAllJobs(roomId = null) {
    const url = roomId
      ? `${this.relayUrl}/api/colab-relay/purge-jobs?room=${encodeURIComponent(roomId)}`
      : `${this.relayUrl}/api/colab-relay/purge-jobs`;
    const res = await fetch(url, {
      method: "POST",
      headers: { "Accept": "application/json" },
    });
    if (!res.ok) {
      throw new Error(`Failed to purge jobs: HTTP ${res.status}`);
    }
    return await res.json();
  }

  async purgeAll() {
    const url = `${this.relayUrl}/api/colab-relay/purge-all`;
    const res = await fetch(url, {
      method: "POST",
      headers: { "Accept": "application/json" },
    });
    if (!res.ok) {
      throw new Error(`Failed to purge all servers: HTTP ${res.status}`);
    }
    return await res.json();
  }
}

export const api = new RelayApiClient();
