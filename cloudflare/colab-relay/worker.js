const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET,POST,DELETE,OPTIONS",
  "access-control-allow-headers": "authorization,content-type,range",
};

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: JSON_HEADERS });

      // Global purge-all endpoint (purges all servers and all jobs)
      if (request.method === "POST" && (url.pathname === "/api/colab-relay/purge-all" || url.pathname === "/api/colab-relay/purge")) {
        return handleColabRelayPurgeAll(request, env);
      }

      // Purge all jobs across all servers (or single room)
      if ((request.method === "POST" || request.method === "DELETE") && (url.pathname === "/api/colab-relay/purge-jobs" || url.pathname === "/api/colab-relay/jobs")) {
        const room = url.searchParams.get("room");
        return handleColabRelayPurgeJobs(request, env, room);
      }

      // Delete server from registry
      if (request.method === "DELETE" && url.pathname.startsWith("/api/colab-relay/servers/")) {
        const room = url.pathname.replace("/api/colab-relay/servers/", "").trim();
        if (room) {
          return handleColabRelayServerDelete(request, env, room);
        }
      }
      if (request.method === "DELETE" && (url.pathname === "/api/colab-relay/servers" || url.pathname === "/api/colab-relay/servers/")) {
        const room = url.searchParams.get("room");
        if (room) {
          return handleColabRelayServerDelete(request, env, room);
        }
      }

      // List all tracked servers (max 10 servers, filtered < 12 hours inactive)
      if (request.method === "GET" && (url.pathname === "/api/colab-relay/servers" || url.pathname === "/api/colab-relay/servers/")) {
        const room = url.searchParams.get("room");
        if (room) {
          return handleColabRelayServerDetail(request, env, room);
        }
        return handleColabRelayServerList(request, env);
      }

      // Get single server details by room
      if (request.method === "GET" && url.pathname.startsWith("/api/colab-relay/servers/")) {
        const room = url.pathname.replace("/api/colab-relay/servers/", "").trim();
        if (room) {
          return handleColabRelayServerDetail(request, env, room);
        }
      }

      if (request.method === "GET" && url.pathname === "/api/colab-relay/ws") return handleColabRelay(request, env);
      if (request.method === "GET" && url.pathname === "/api/colab-relay/health") return json(await handleColabRelayHealth(request, env));

      return json({ error: "not_found", message: "Route not found" }, 404);
    } catch (err) {
      const status = Number(err.status || 500);
      return json({ error: err.code || "internal_error", message: err.safeMessage || err.message || "Relay error" }, status);
    }
  },
};

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

function fail(status, code, message) {
  const err = new Error(message);
  err.status = status;
  err.code = code;
  err.safeMessage = message;
  throw err;
}

function cleanRoom(room) {
  return String(room || "").replace(/[^a-zA-Z0-9_.:-]/g, "").slice(0, 128);
}

function requireColabRelay(request, env) {
  const room = new URL(request.url).searchParams.get("room") || "";
  const clean = cleanRoom(room);
  if (!clean) fail(400, "room_required", "room is required");
  if (!env.COLAB_RELAY) fail(500, "server_misconfigured", "COLAB_RELAY binding is required");
  return clean;
}

function handleColabRelay(request, env) {
  const room = requireColabRelay(request, env);
  return env.COLAB_RELAY.getByName(room).fetch(request);
}

async function handleColabRelayHealth(request, env) {
  const room = requireColabRelay(request, env);
  const token = new URL(request.url).searchParams.get("token") || "";
  const res = await env.COLAB_RELAY.getByName(room).fetch(new Request(`https://room/health?token=${encodeURIComponent(token)}`));
  return await res.json();
}

async function handleColabRelayServerList(request, env) {
  if (!env.COLAB_RELAY) fail(500, "server_misconfigured", "COLAB_RELAY binding is required");
  const res = await env.COLAB_RELAY.getByName("__registry__").fetch(new Request("https://registry/servers"));
  return json(await res.json());
}

async function handleColabRelayServerDetail(request, env, rawRoom) {
  if (!env.COLAB_RELAY) fail(500, "server_misconfigured", "COLAB_RELAY binding is required");
  const room = cleanRoom(rawRoom);
  if (!room) fail(400, "room_required", "room is required");
  const res = await env.COLAB_RELAY.getByName(room).fetch(new Request("https://room/detail"));
  return json(await res.json());
}

async function handleColabRelayServerDelete(request, env, rawRoom) {
  if (!env.COLAB_RELAY) fail(500, "server_misconfigured", "COLAB_RELAY binding is required");
  const room = cleanRoom(rawRoom);
  if (!room) fail(400, "room_required", "room is required");

  // 1. Purge room state and close all WebSockets on room
  try {
    await env.COLAB_RELAY.getByName(room).fetch(new Request("https://room/purge", { method: "POST" }));
  } catch {}

  // 2. Delete permanently from __registry__
  const res = await env.COLAB_RELAY.getByName("__registry__").fetch(new Request(`https://registry/servers/${encodeURIComponent(room)}`, { method: "DELETE" }));

  return json(await res.json());
}

async function handleColabRelayPurgeJobs(request, env, rawRoom) {
  if (!env.COLAB_RELAY) fail(500, "server_misconfigured", "COLAB_RELAY binding is required");
  if (rawRoom) {
    const room = cleanRoom(rawRoom);
    if (!room) fail(400, "room_required", "room is required");
    try {
      await env.COLAB_RELAY.getByName(room).fetch(new Request("https://room/purge-jobs", { method: "POST" }));
    } catch {}
    const res = await env.COLAB_RELAY.getByName("__registry__").fetch(new Request(`https://registry/purge-room-jobs/${encodeURIComponent(room)}`, { method: "POST" }));
    return json(await res.json());
  }

  const res = await env.COLAB_RELAY.getByName("__registry__").fetch(new Request("https://registry/purge-all-jobs", { method: "POST" }));
  return json(await res.json());
}

async function handleColabRelayPurgeAll(request, env) {
  if (!env.COLAB_RELAY) fail(500, "server_misconfigured", "COLAB_RELAY binding is required");
  const res = await env.COLAB_RELAY.getByName("__registry__").fetch(new Request("https://registry/purge-all", { method: "POST" }));
  return json(await res.json());
}

const TWELVE_HOURS_MS = 12 * 60 * 60 * 1000;
const MAX_SERVERS_LIMIT = 10;

export class ColabRelayRoom {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.roomName = "";
    this.snapshot = null;
    this.token = "";
    this.lastPersist = 0;
    this.lastActive = Date.now();
    this.environment = "Colab";
    this.serverName = "";
    this.meta = {};
    this.jobsMap = new Map();

    // Registry specific state
    this.serversMap = new Map();
    this.deletedRooms = new Set();

    state.blockConcurrencyWhile(async () => {
      this.snapshot = await state.storage.get("snapshot").catch(() => null);
      this.token = (await state.storage.get("token").catch(() => "")) || "";
      this.lastActive = (await state.storage.get("lastActive").catch(() => 0)) || Date.now();
      this.environment = (await state.storage.get("environment").catch(() => "Colab")) || "Colab";
      this.serverName = (await state.storage.get("serverName").catch(() => "")) || "";
      this.meta = (await state.storage.get("meta").catch(() => ({}))) || {};

      const storedJobs = (await state.storage.get("jobsMap").catch(() => null)) || {};
      for (const [k, v] of Object.entries(storedJobs)) {
        this.jobsMap.set(k, v);
      }

      const storedServers = (await state.storage.get("serversMap").catch(() => null)) || {};
      for (const [k, v] of Object.entries(storedServers)) {
        this.serversMap.set(k, v);
      }

      const storedDeleted = (await state.storage.get("deletedRooms").catch(() => null)) || [];
      if (Array.isArray(storedDeleted)) {
        this.deletedRooms = new Set(storedDeleted);
      }
    });
  }

  getFilteredServersList() {
    const now = Date.now();
    const list = [];
    const keysToDelete = [];

    for (const [roomId, s] of this.serversMap.entries()) {
      // If server is in deleted tombstone list, clean it out
      if (this.deletedRooms.has(roomId)) {
        keysToDelete.push(roomId);
        continue;
      }

      const isOffline = !s.online;
      const inactiveMs = now - (s.lastActive || 0);

      // Filter out servers offline for more than 12 hours
      if (isOffline && inactiveMs > TWELVE_HOURS_MS) {
        keysToDelete.push(roomId);
        continue;
      }

      list.push({
        ...s,
        inactiveMs,
        expiresInMs: isOffline ? Math.max(0, TWELVE_HOURS_MS - inactiveMs) : null,
      });
    }

    if (keysToDelete.length > 0) {
      for (const k of keysToDelete) {
        this.serversMap.delete(k);
      }
      this.persistRegistry();
    }

    // Stable Sort: Online servers first, then sorted by firstSeen (creation order) so cards NEVER jump
    list.sort((a, b) => {
      if (a.online && !b.online) return -1;
      if (!a.online && b.online) return 1;
      return (a.firstSeen || 0) - (b.firstSeen || 0) || String(a.roomId).localeCompare(String(b.roomId));
    });

    // Enforce maximum 10 servers limit
    const cappedList = list.slice(0, MAX_SERVERS_LIMIT);

    // Auto-prune offline servers beyond the top 10 from registry storage
    if (list.length > MAX_SERVERS_LIMIT) {
      const keptKeys = new Set(cappedList.map((s) => s.roomId));
      let modified = false;
      for (const [rid, s] of this.serversMap.entries()) {
        if (!keptKeys.has(rid) && !s.online) {
          this.serversMap.delete(rid);
          modified = true;
        }
      }
      if (modified) {
        this.persistRegistry();
      }
    }

    return {
      ok: true,
      count: cappedList.length,
      maxLimit: MAX_SERVERS_LIMIT,
      onlineCount: cappedList.filter((s) => s.online).length,
      offlineCount: cappedList.filter((s) => !s.online).length,
      servers: cappedList,
      timestamp: now,
    };
  }

  async fetch(request) {
    const url = new URL(request.url);

    // ==========================================
    // REGISTRY INTERNAL HANDLERS
    // ==========================================
    if (url.pathname === "/servers" && request.method === "GET") {
      const result = this.getFilteredServersList();
      return json(result);
    }

    if (request.method === "DELETE" && url.pathname.startsWith("/servers/")) {
      const roomToDelete = decodeURIComponent(url.pathname.replace("/servers/", "").trim());
      if (roomToDelete) {
        this.serversMap.delete(roomToDelete);
        this.deletedRooms.add(roomToDelete);
        await this.persistRegistry();

        const listData = this.getFilteredServersList();
        this.broadcast({ type: "servers_update", ...listData });
        return json({ ok: true, deleted: roomToDelete });
      }
    }

    if (url.pathname === "/purge-all-jobs" && request.method === "POST") {
      // Clear jobs in all registered servers
      for (const [roomId, s] of this.serversMap.entries()) {
        s.jobs = [];
        s.latestJob = null;
        s.activeJobs = 0;
        s.totalJobs = 0;
        try {
          await this.env.COLAB_RELAY.getByName(roomId).fetch(new Request("https://room/purge-jobs", { method: "POST" }));
        } catch {}
      }
      await this.persistRegistry();
      const listData = this.getFilteredServersList();
      this.broadcast({ type: "servers_update", ...listData });
      return json({ ok: true, purgedAllJobs: true });
    }

    if (url.pathname.startsWith("/purge-room-jobs/") && request.method === "POST") {
      const room = decodeURIComponent(url.pathname.replace("/purge-room-jobs/", "").trim());
      if (room && this.serversMap.has(room)) {
        const s = this.serversMap.get(room);
        s.jobs = [];
        s.latestJob = null;
        s.activeJobs = 0;
        s.totalJobs = 0;
        await this.persistRegistry();
        const listData = this.getFilteredServersList();
        this.broadcast({ type: "servers_update", ...listData });
      }
      return json({ ok: true, purgedRoomJobs: room });
    }

    if (url.pathname === "/purge-all" && request.method === "POST") {
      // Close and purge all room Durable Objects
      for (const roomId of this.serversMap.keys()) {
        try {
          await this.env.COLAB_RELAY.getByName(roomId).fetch(new Request("https://room/purge", { method: "POST" }));
        } catch {}
      }
      this.serversMap.clear();
      this.deletedRooms.clear();
      await this.state.storage.deleteAll();
      const listData = this.getFilteredServersList();
      this.broadcast({ type: "servers_update", ...listData });
      return json({ ok: true, purgedAll: true });
    }

    // Room specific purge endpoints
    if (url.pathname === "/purge" && request.method === "POST") {
      // Terminate any active WebSockets so no ghost syncs occur
      for (const ws of this.state.getWebSockets()) {
        try {
          ws.close(1000, "purged");
        } catch {}
      }
      await this.state.storage.deleteAll();
      this.snapshot = null;
      this.jobsMap.clear();
      this.serverName = "";
      this.environment = "Colab";
      this.meta = {};
      return json({ ok: true, purged: true });
    }

    if (url.pathname === "/purge-jobs" && request.method === "POST") {
      this.snapshot = null;
      this.jobsMap.clear();
      await this.state.storage.delete("snapshot");
      await this.state.storage.delete("jobsMap");
      // Broadcast cleared snapshot to app clients
      this.sendRole("app", { type: "snapshot", jobId: "", job: null, environment: this.environment });
      return json({ ok: true, purgedJobs: true });
    }

    if (url.pathname === "/update-registry" && request.method === "POST") {
      const data = await request.json().catch(() => ({}));
      if (data.roomId && data.roomId !== "__registry__") {
        // If room was explicitly deleted, only re-allow if colab is online
        if (this.deletedRooms.has(data.roomId)) {
          if (data.online) {
            this.deletedRooms.delete(data.roomId);
          } else {
            return json({ ok: false, reason: "room_deleted" });
          }
        }

        // If server is offline and has 0 jobs and no name, do not create ghost entry
        if (!data.online && (!data.jobs || data.jobs.length === 0) && (!data.totalJobs || data.totalJobs === 0) && !this.serversMap.has(data.roomId)) {
          return json({ ok: false, reason: "ghost_server_ignored" });
        }

        const existing = this.serversMap.get(data.roomId) || {};
        const now = Date.now();
        const updated = {
          ...existing,
          ...data,
          firstSeen: existing.firstSeen || now,
          lastActive: data.lastActive || now,
        };
        this.serversMap.set(data.roomId, updated);
        await this.persistRegistry();

        // Broadcast to all WebSocket clients connected to registry
        const listData = this.getFilteredServersList();
        this.broadcast({ type: "servers_update", ...listData });
      }
      return json({ ok: true });
    }

    // ==========================================
    // ROOM DETAIL & HEALTH HANDLERS
    // ==========================================
    if (url.pathname === "/health") {
      const token = url.searchParams.get("token") || "";
      if (this.token && token && token !== this.token) return json({ error: "invalid_token" }, 401);
      return json({
        ok: true,
        sockets: this.state.getWebSockets().length,
        colabReady: this.hasRole("colab"),
        snapshot: this.snapshot,
        lastActive: this.lastActive,
        environment: this.environment,
        serverName: this.serverName,
      });
    }

    if (url.pathname === "/detail") {
      const jobs = Array.from(this.jobsMap.values());
      const snapJob = this.snapshot?.job;
      const snapId = this.snapshot?.jobId || snapJob?.jobId || snapJob?.id || snapJob?.job_id;
      if (snapJob && snapId && !jobs.some((j) => (j.jobId || j.id || j.job_id) === snapId)) {
        jobs.unshift(snapJob);
      }
      return json({
        ok: true,
        roomId: this.roomName,
        online: this.hasRole("colab"),
        colabReady: this.hasRole("colab"),
        sockets: this.state.getWebSockets().length,
        environment: this.environment,
        serverName: this.serverName || this.roomName,
        lastActive: this.lastActive,
        snapshot: this.snapshot,
        jobs: jobs,
        logs: snapJob?.logs || this.snapshot?.logs || [],
      });
    }

    // ==========================================
    // WEBSOCKET HANDLERS
    // ==========================================
    if (request.headers.get("Upgrade") !== "websocket") return json({ error: "websocket_required" }, 426);

    const roomParam = cleanRoom(url.searchParams.get("room") || "");
    if (roomParam) this.roomName = roomParam;

    const token = url.searchParams.get("token") || "";
    if (token) {
      if (!this.token) {
        this.token = token;
        await this.state.storage.put("token", token);
      } else if (token !== this.token) {
        const requestedRole = url.searchParams.get("role");
        if (requestedRole !== "app") {
          return json({ error: "invalid_token" }, 401);
        }
      }
    }

    const role = url.searchParams.get("role") === "colab" ? "colab" : "app";
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    server.serializeAttachment({ role });
    this.state.acceptWebSocket(server);

    this.lastActive = Date.now();
    await this.state.storage.put("lastActive", this.lastActive);

    // If this is the registry room, send immediate server list snapshot
    if (this.roomName === "__registry__") {
      const listData = this.getFilteredServersList();
      server.send(JSON.stringify({ type: "servers_update", role: "app", ...listData }));
      return new Response(null, { status: 101, webSocket: client });
    }

    server.send(JSON.stringify({ type: "ready", role, colabReady: this.hasRole("colab") || role === "colab" }));
    if (role === "app") {
      // Send all existing jobs snapshots to newly connected app client
      for (const [jid, jData] of this.jobsMap.entries()) {
        server.send(JSON.stringify({ type: "snapshot", jobId: jid, job: jData, environment: this.environment }));
      }
      if (this.snapshot && !this.jobsMap.has(this.snapshot.jobId)) {
        server.send(JSON.stringify({ type: "snapshot", ...this.snapshot }));
      }
    }

    this.broadcast({ type: "ready", colabReady: this.hasRole("colab") || role === "colab" });

    // Notify registry ONLY if colab is online or jobs exist
    if (this.hasRole("colab") || this.jobsMap.size > 0 || this.snapshot) {
      await this.syncToRegistry();
    }

    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws, raw) {
    let msg = {};
    try {
      msg = JSON.parse(String(raw || "{}"));
    } catch {
      return;
    }

    const role = ws.deserializeAttachment()?.role || "app";
    this.lastActive = Date.now();

    if (role === "app" && ["start_transfer", "confirm", "cancel"].includes(msg.type)) {
      if (!this.hasRole("colab")) {
        ws.send(JSON.stringify({ type: "error", jobId: msg.jobId || "", error: { code: "COLAB_NOT_CONNECTED", message: "Colab runtime is not connected to Cloudflare server" } }));
        return;
      }
      this.sendRole("colab", msg);
      return;
    }

    if (role !== "colab") return;

    if (msg.type === "hello") {
      if (msg.environment) this.environment = String(msg.environment);
      if (msg.serverName) this.serverName = String(msg.serverName);
      await this.state.storage.put("environment", this.environment);
      if (this.serverName) await this.state.storage.put("serverName", this.serverName);
      await this.syncToRegistry();
    }

    if (msg.type === "heartbeat") {
      if (msg.environment) this.environment = String(msg.environment);
      await this.syncToRegistry();
      return;
    }

    if (["progress", "snapshot", "done", "error"].includes(msg.type)) {
      this.snapshot = redactColabSnapshot(msg);
      const jid = msg.jobId || msg.job?.jobId || msg.job?.id || msg.job?.job_id;
      if (jid && msg.job) {
        this.jobsMap.set(String(jid), redactColabSnapshot(msg.job));
      }
      const terminal = ["done", "error"].includes(msg.type) || ["completed", "failed", "cancelled"].includes(String(msg.job?.status || msg.status || ""));
      if (terminal || Date.now() - this.lastPersist > 3000) {
        this.lastPersist = Date.now();
        await this.state.storage.put("snapshot", this.snapshot);
        await this.state.storage.put("lastActive", this.lastActive);
        await this.state.storage.put("jobsMap", Object.fromEntries(this.jobsMap));
      }
      await this.syncToRegistry();
    }

    this.sendRole("app", msg);
  }

  async webSocketClose() {
    this.lastActive = Date.now();
    await this.state.storage.put("lastActive", this.lastActive);
    this.broadcast({ type: "ready", colabReady: this.hasRole("colab") });
    if (this.hasRole("colab") || this.jobsMap.size > 0 || this.snapshot) {
      await this.syncToRegistry();
    }
  }

  async webSocketError() {
    this.lastActive = Date.now();
    await this.state.storage.put("lastActive", this.lastActive);
    this.broadcast({ type: "ready", colabReady: this.hasRole("colab") });
    if (this.hasRole("colab") || this.jobsMap.size > 0 || this.snapshot) {
      await this.syncToRegistry();
    }
  }

  hasRole(role) {
    return this.state.getWebSockets().some((ws) => ws.deserializeAttachment()?.role === role);
  }

  sendRole(role, msg) {
    for (const ws of this.state.getWebSockets()) {
      if (ws.deserializeAttachment()?.role === role) ws.send(JSON.stringify(msg));
    }
  }

  broadcast(msg) {
    const raw = JSON.stringify(msg);
    for (const ws of this.state.getWebSockets()) ws.send(raw);
  }

  async syncToRegistry() {
    if (!this.env?.COLAB_RELAY || !this.roomName || this.roomName === "__registry__") return;
    const isColabOnline = this.hasRole("colab");
    const hasJobData = this.jobsMap.size > 0 || (this.snapshot && this.snapshot.job);

    // If colab is not online and no job data and no serverName, do not ghost sync
    if (!isColabOnline && !hasJobData && !this.serverName) return;

    try {
      const jobsList = Array.from(this.jobsMap.values());
      const activeCount = jobsList.filter((j) => ["pending", "running", "downloading", "uploading", "extracting", "optimizing", "waiting_confirmation"].includes(String(j.status || "").toLowerCase())).length;

      const latestJob = this.snapshot?.job || (jobsList.length > 0 ? jobsList[jobsList.length - 1] : null);

      const payload = {
        roomId: this.roomName,
        name: this.serverName || (this.environment ? `VaultBox ${this.environment} Worker` : this.roomName),
        environment: this.environment || "Colab",
        online: isColabOnline,
        colabReady: isColabOnline,
        sockets: this.state.getWebSockets().length,
        lastActive: this.lastActive,
        activeJobs: activeCount,
        totalJobs: jobsList.length,
        jobs: jobsList.map((j) => ({
          id: j.id || j.job_id || j.jobId,
          jobId: j.id || j.job_id || j.jobId,
          name: j.name || j.current_file || j.currentFile || "Transfer Job",
          status: j.status || "idle",
          step: j.step || "",
          progress: j.progress || 0,
          speed: j.speed || 0,
          message: j.message || "",
        })),
        latestJob: latestJob
          ? {
              id: latestJob.id || latestJob.job_id || latestJob.jobId,
              jobId: latestJob.id || latestJob.job_id || latestJob.jobId,
              name: latestJob.name || latestJob.current_file || latestJob.currentFile || "Transfer Job",
              status: latestJob.status || "idle",
              step: latestJob.step || "",
              progress: latestJob.progress || 0,
              speed: latestJob.speed || 0,
              message: latestJob.message || "",
            }
          : null,
      };

      await this.env.COLAB_RELAY.getByName("__registry__").fetch(
        new Request("https://registry/update-registry", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        })
      );
    } catch {
      // Best effort registry sync
    }
  }

  async persistRegistry() {
    if (this.roomName === "__registry__" || !this.roomName) {
      await this.state.storage.put("serversMap", Object.fromEntries(this.serversMap));
      await this.state.storage.put("deletedRooms", Array.from(this.deletedRooms));
    }
  }
}

function redactColabSnapshot(msg) {
  const clone = JSON.parse(JSON.stringify(msg || {}));
  const scrub = (value) => {
    if (!value || typeof value !== "object") return;
    for (const key of Object.keys(value)) {
      if (["credentials", "access_token", "refresh_token", "cookies", "auth_headers", "api_keys", "encoded_token"].includes(key)) delete value[key];
      else scrub(value[key]);
    }
  };
  scrub(clone);
  if (clone.job?.logs?.length > 500) clone.job.logs = clone.job.logs.slice(-500);
  if (clone.logs?.length > 500) clone.logs = clone.logs.slice(-500);
  return clone;
}

export const __test = { redactColabSnapshot };
