# VaultBox Colab Transfer Worker

Google Colab is only a temporary worker:

```txt
source provider -> Colab temp storage -> optional extract -> target provider
```

VaultBox remains the controller. Provider credentials are sent per job/session by the app, never typed into the notebook.

## Run

1. Open `server_launcher.ipynb` in Google Colab.
2. Run all cells.
3. Copy `Server URL` and `Connection Token` into VaultBox Settings / Connect Colab.
4. Start transfers from VaultBox.

## API

All endpoints except `/health` require:

```txt
Authorization: Bearer <COLAB_SERVER_TOKEN>
```

Core endpoints:

```txt
GET  /health
POST /connect/verify
GET  /providers
POST /providers/{provider}/validate
POST /transfer/start
GET  /transfer/status/{jobId}
GET  /transfer/list
POST /transfer/cancel/{jobId}
GET  /transfer/logs/{jobId}
GET  /events/{jobId}
```

## Security

- Random `COLAB_SERVER_TOKEN` per runtime unless explicitly set.
- Provider credentials are accepted in job payload only.
- Credentials are not logged and are removed from job payload after completion.
- Temp files live under `/content/vaultbox_tmp/jobs/<jobId>` and are cleaned by default.
- Path joins are constrained to worker temp dirs.

## Providers

- `terabox`: ported from current VaultBox HTTP flow: `ndus` cookie, region discovery, `/api/list`, `/api/filemetas`, `/api/precreate`, `/rest/2.0/pcs/superfile2`, `/api/create`.
- `pikpak`: uses app-compatible access/refresh/encoded token payload; direct download URL resolution and OSS resumable upload.
- `drive`: uses app-provided Google access token/web token for Drive API download/upload.

## Transfer Payload

```json
{
  "source": {
    "provider": "terabox",
    "accountId": "tb_...",
    "credentials": {"cookies": {"ndus": "..."}},
    "items": [{"id": "/path/file.zip", "path": "/path/file.zip", "name": "file.zip", "type": "file"}]
  },
  "target": {
    "provider": "pikpak",
    "accountId": "pp_...",
    "credentials": {"encoded_token": "...", "device_id": "..."},
    "folder": {"id": ""}
  },
  "options": {
    "extract": true,
    "archivePassword": null,
    "preserveFolderStructure": true,
    "cleanupAfterFinish": true
  }
}
```
