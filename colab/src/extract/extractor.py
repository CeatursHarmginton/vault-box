from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from ..jobs.progress import JobState
from ..providers.base import ProviderFailure

ARCHIVE_EXTS = (".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".iso")

def is_archive_name(name: str) -> bool:
    name = name.lower()
    if re.search(r"\.\d{3}$", name):
        return name.endswith(".001")
    if re.search(r"\.r\d{2}$", name):
        return False
    return name.endswith(ARCHIVE_EXTS)

def _is_first_volume(name: str) -> bool:
    name = name.lower()
    m = re.search(r"\.part(\d+)\.rar$", name)
    if m:
        return int(m.group(1)) == 1
    m = re.search(r"\.(\d{3})$", name)
    if m:
        return int(m.group(1)) == 1
    return is_archive_name(name)

def archives(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and _is_first_volume(p.name)]

def _check_multipart(first: Path) -> None:
    m = re.search(r"^(.*)\.part0*1\.rar$", first.name, re.I)
    if not m:
        return
    prefix = m.group(1)
    siblings = {p.name.lower() for p in first.parent.glob(f"{prefix}.part*.rar")}
    idx = 1
    while f"{prefix}.part{idx}.rar".lower() in siblings or f"{prefix}.part{idx:02d}.rar".lower() in siblings:
        idx += 1
    # 7z catches most missing middle parts too; this catches obvious single-part gaps.
    if idx == 1:
        raise ProviderFailure("ARCHIVE_PART_MISSING", f"Missing multipart RAR first part near {first.name}")

async def extract_archives(input_dir: Path, output_dir: Path, progress: JobState, password: str | list[str] | None = None, delete_archive: bool = False) -> list[Path]:
    if not shutil.which("7z"):
        raise ProviderFailure("EXTRACT_FAILED", "7z not installed. In Colab run: apt-get install -y p7zip-full unrar")
    found = archives(input_dir)
    if not found:
        return [p for p in input_dir.rglob("*") if p.is_file()]
    output_dir.mkdir(parents=True, exist_ok=True)
    total = len(found)

    # Build candidates
    pw_candidates: list[str | None] = []
    if isinstance(password, list):
        for pw in password:
            if pw and pw not in pw_candidates:
                pw_candidates.append(pw)
    elif isinstance(password, str) and password:
        pw_candidates.append(password)

    if not pw_candidates:
        pw_candidates = [None]
    else:
        if None not in pw_candidates:
            pw_candidates.append(None)

    for i, archive in enumerate(found, 1):
        progress.check_cancelled()
        _check_multipart(archive)
        progress.set(step="extracting", current_file=archive.name)

        extracted = False
        last_error_text = ""
        if len(pw_candidates) > 1:
            progress.log(f"Archive password candidates: {len(pw_candidates) - 1}; trying all, then no-password fallback")
        for attempt, pw in enumerate(pw_candidates):
            cmd = ["7z", "x", "-y", f"-o{output_dir}", str(archive)]
            if pw:
                cmd.insert(2, f"-p{pw}")
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            text = out.decode(errors="ignore")
            if proc.returncode == 0:
                extracted = True
                break
            last_error_text = text
            lowered = text.lower()
            if "wrong password" in lowered or "encrypted" in lowered or "cannot open encrypted" in lowered:
                continue
            if "volume" in lowered and "missing" in lowered:
                raise ProviderFailure("ARCHIVE_PART_MISSING", text[-500:])
            if attempt < len(pw_candidates) - 1:
                continue
            break

        if not extracted:
            lowered = last_error_text.lower()
            if "wrong password" in lowered or "encrypted" in lowered or "cannot open encrypted" in lowered:
                raise ProviderFailure("ARCHIVE_PASSWORD_REQUIRED", "Archive password required or invalid")
            if "volume" in lowered and "missing" in lowered:
                raise ProviderFailure("ARCHIVE_PART_MISSING", last_error_text[-500:])
            raise ProviderFailure("EXTRACT_FAILED", last_error_text[-500:] or "Failed to extract archive")

        progress.progress.extract = i / total * 100
        if delete_archive:
            archive.unlink(missing_ok=True)
    return [p for p in output_dir.rglob("*") if p.is_file()]
