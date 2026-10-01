from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
repo_root = ROOT.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.config import (  # noqa: E402
    HOST,
    PORT,
    SERVER_TOKEN,
    NO_AUTH,
    IS_KAGGLE,
    IS_COLAB,
    IS_LOCAL,
    ENV_NAME,
    AUTO_STOP_ENABLED,
    AUTO_STOP_IDLE_MINUTES,
)

is_local = os.environ.get("COLAB_LOCAL") == "1" or NO_AUTH or (not IS_KAGGLE and not IS_COLAB)

if not is_local and os.name != "nt" and (Path("/content").exists() or Path("/kaggle").exists()):
    try:
        import subprocess
        if (repo_root / ".git").exists():
            subprocess.run(["git", "checkout", "."], cwd=str(repo_root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            res = subprocess.run(["git", "pull", "--rebase"], cwd=str(repo_root), capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                print("[*] Code updated from GitHub (git pull successful).", flush=True)
    except Exception:
        pass


def _auto_connect_vaultbox_worker(host: str, port: int) -> None:
    """Auto-connect background worker for local development."""
    import httpx

    target_host = "127.0.0.1" if host in {"0.0.0.0", "127.0.0.1", "localhost"} else host
    colab_url = f"http://{target_host}:{port}"

    ready = False
    for _ in range(40):
        try:
            with httpx.Client(timeout=1.0) as client:
                resp = client.get(f"{colab_url}/health")
                if resp.status_code == 200:
                    ready = True
                    break
        except Exception:
            pass
        time.sleep(0.5)

    if not ready:
        print("[!] Local Colab worker failed to respond to health check in 20s.", flush=True)
        return

    print(f"\n[+] Local Colab Worker is READY at {colab_url}", flush=True)

    # 1. Update VaultBox persistent settings.json
    try:
        from backend.app.core.app_settings import get_settings, update_settings
        settings = get_settings()
        servers = list(settings.get("colab_servers") or [])
        local_id = "colab_local"
        local_entry = {
            "id": local_id,
            "name": "Local Colab",
            "mode": "tunnel",
            "url": colab_url,
            "token": "",
            "server_url": "https://vaultbox-colab-relay.lefantasusdelimei.workers.dev",
            "server_token": "",
            "room_id": "",
            "worker_url": colab_url,
            "api_token": "",
            "updated_at": time.time(),
        }

        found = False
        new_servers = []
        for s in servers:
            if str(s.get("id") or "") == local_id or str(s.get("name") or "").strip().lower() == "local colab":
                new_servers.append({**s, **local_entry})
                found = True
            else:
                new_servers.append(s)
        if not found:
            local_entry["created_at"] = time.time()
            new_servers.append(local_entry)

        update_settings({
            "colab_servers": new_servers,
            "colab_servers_migrated": True,
        })
        print("[+] Registered 'Local Colab' in VaultBox settings.", flush=True)
    except Exception as exc:
        print(f"[*] Note: Could not write directly to settings.json ({exc})", flush=True)

    # 2. Ping running VaultBox instances across dev/production ports (8765-8775)
    connected_ports = []
    for vb_port in range(8765, 8776):
        vb_url = f"http://127.0.0.1:{vb_port}"
        try:
            with httpx.Client(timeout=3.0) as client:
                ping = client.get(f"{vb_url}/api/colab/status")
                if ping.status_code == 200:
                    payload = {
                        "id": "colab_local",
                        "name": "Local Colab",
                        "mode": "tunnel",
                        "url": colab_url,
                        "token": "",
                        "verify": True,
                    }
                    client.post(f"{vb_url}/api/colab/servers", json=payload)
                    connected_ports.append(vb_port)
        except Exception:
            pass

    if connected_ports:
        port_list = ", ".join(str(p) for p in connected_ports)
        print(f"[OK] AUTO-CONNECTED to active VaultBox on port(s): {port_list}!", flush=True)
        print("     -> VaultBox UI is now connected and ready for transfer jobs with Local Colab!\n", flush=True)
    else:
        print("[i] VaultBox app is not currently open.", flush=True)
        print("    -> When you launch VaultBox, Local Colab will be automatically connected and ready!\n", flush=True)


if __name__ == "__main__":
    print("==================================================================", flush=True)
    print(f"      VaultBox Worker Server ({ENV_NAME} Environment)     ", flush=True)
    print("==================================================================", flush=True)
    if IS_KAGGLE:
        print("[+] Environment: Kaggle Container detected.", flush=True)
        if AUTO_STOP_ENABLED:
            print(f"[+] Kaggle Auto-Stop: Active (Will automatically shutdown after {AUTO_STOP_IDLE_MINUTES}m idle without active jobs to save quota).", flush=True)
    elif IS_COLAB:
        print("[+] Environment: Google Colab detected (Continuous keepalive active).", flush=True)
    elif is_local:
        print("[+] Environment: Local PC worker.", flush=True)
        if NO_AUTH:
            print("[*] Authentication: Disabled (Direct local connection, no token needed)", flush=True)
        threading.Thread(target=_auto_connect_vaultbox_worker, args=(HOST, PORT), daemon=True).start()
    else:
        print(f"Connection Token: {SERVER_TOKEN}", flush=True)

    print(f"[*] Starting server on {HOST}:{PORT}...", flush=True)
    uvicorn.run("src.server:app", host=HOST, port=PORT, reload=False)
