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

def _archive_originals(first: Path) -> list[Path]:
    name = first.name
    m = re.search(r"^(.*)\.part0*1\.rar$", name, re.I)
    if m:
        return sorted(first.parent.glob(f"{m.group(1)}.part*.rar"))
    m = re.search(r"^(.*)\.001$", name, re.I)
    if m:
        return sorted(p for p in first.parent.glob(f"{m.group(1)}.*") if re.search(r"\.\d{3}$", p.name))
    if first.suffix.lower() == ".rar":
        return [first, *sorted(first.parent.glob(f"{first.stem}.r[0-9][0-9]"))]
    return [first]

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
    archive_originals = {p for archive in found for p in _archive_originals(archive)}
    keep_originals: list[Path] = []

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
        progress.set(step="extracting", current_file=archive.name)
        before = {p for p in output_dir.rglob("*") if p.is_file()}

        extracted = False
        last_error_text = ""
        try:
            _check_multipart(archive)
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
                if "volume" in lowered and "missing" in lowered:
                    raise ProviderFailure("ARCHIVE_PART_MISSING", text[-500:])
                if attempt < len(pw_candidates) - 1:
                    continue
                break
        except ProviderFailure as exc:
            last_error_text = exc.message

        if not extracted:
            originals = _archive_originals(archive)
            keep_originals.extend(p for p in originals if p.is_file() and p not in keep_originals)
            progress.log(f"[SKIP] Extract failed, uploading original archive: {archive.name}")
            for p in set(output_dir.rglob("*")) - before:
                if p.is_file():
                    p.unlink(missing_ok=True)
            progress.progress.extract = i / total * 100
            continue

        progress.progress.extract = i / total * 100
        if delete_archive:
            for p in _archive_originals(archive):
                p.unlink(missing_ok=True)
    extracted_files = [p for p in output_dir.rglob("*") if p.is_file()]
    passthrough = [p for p in input_dir.rglob("*") if p.is_file() and p not in archive_originals]
    return extracted_files + passthrough + keep_originals
