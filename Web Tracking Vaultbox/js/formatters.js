/**
 * VaultBox Formatters & Utility Functions
 */

export function escapeHtml(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

export function getJobId(job) {
  if (!job) return "";
  return String(job.jobId || job.id || job.job_id || "");
}

export function formatBytes(bytes, decimals = 2) {
  if (!+bytes || bytes <= 0) return "0 B";
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
}

export function formatSpeed(bytesPerSec) {
  if (!+bytesPerSec || bytesPerSec <= 0) return "0 MB/s";
  return `${formatBytes(bytesPerSec, 2)}/s`;
}

export function formatDuration(seconds) {
  if (!seconds || seconds <= 0) return "0s";
  const sec = Math.floor(seconds);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;

  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export function formatStopwatch(seconds) {
  if (!seconds || seconds <= 0) return "00:00";
  const sec = Math.floor(seconds);
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export function formatRelativeTime(timestampMs) {
  if (!timestampMs) return "Never";
  const diffSec = Math.floor((Date.now() - timestampMs) / 1000);
  if (diffSec < 5) return "Just now";
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ${diffMin % 60}m ago`;
  const diffDay = Math.floor(diffHour / 24);
  return `${diffDay}d ago`;
}

export function formatExpiresIn(expiresInMs) {
  if (expiresInMs == null) return "";
  if (expiresInMs <= 0) return "Expired (Hidden soon)";
  const diffSec = Math.floor(expiresInMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour > 0) return `Expires in ${diffHour}h ${diffMin % 60}m`;
  if (diffMin > 0) return `Expires in ${diffMin}m`;
  return `Expires in ${diffSec}s`;
}
