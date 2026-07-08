from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from ..jobs.progress import JobState
from ..providers.base import ProviderFailure

ARCHIVE_EXTS = (".zip", ".7z", ".rar")

def archives(root: Path) -> list[Path]:
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in ARCHIVE_EXTS]
    first = []
    for p in files:
        name = p.name.lower()
        if re.search(r"\.part0*1\.rar$", name) or not re.search(r"\.part\d+\.rar$", name):
            first.append(p)
    return first

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

async def extract_archives(input_dir: Path, output_dir: Path, progress: JobState, password: str | None = None, delete_archive: bool = False) -> list[Path]:
    if not shutil.which("7z"):
        raise ProviderFailure("EXTRACT_FAILED", "7z not installed. In Colab run: apt-get install -y p7zip-full unrar")
    found = archives(input_dir)
    if not found:
        return [p for p in input_dir.rglob("*") if p.is_file()]
    output_dir.mkdir(parents=True, exist_ok=True)
    total = len(found)
    for i, archive in enumerate(found, 1):
        progress.check_cancelled()
        _check_multipart(archive)
        progress.set(step="extracting", current_file=archive.name)
        cmd = ["7z", "x", "-y", f"-o{output_dir}", str(archive)]
        if password:
            cmd.insert(2, f"-p{password}")
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        text = out.decode(errors="ignore")
        if proc.returncode:
            lowered = text.lower()
            if "wrong password" in lowered or "encrypted" in lowered:
                raise ProviderFailure("ARCHIVE_PASSWORD_REQUIRED", "Archive password required or invalid")
            if "volume" in lowered and "missing" in lowered:
                raise ProviderFailure("ARCHIVE_PART_MISSING", text[-500:])
            raise ProviderFailure("EXTRACT_FAILED", text[-500:] or "Failed to extract archive")
        progress.progress.extract = i / total * 100
        if delete_archive:
            archive.unlink(missing_ok=True)
    return [p for p in output_dir.rglob("*") if p.is_file()]
