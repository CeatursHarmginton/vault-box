from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from ..jobs.progress import JobState
from ..providers.base import ProviderFailure, safe_name

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

def _matching_siblings(first: Path, pattern: str) -> list[Path]:
    rx = re.compile(pattern, re.I)
    return sorted(p for p in first.parent.iterdir() if p.is_file() and rx.match(p.name))

def _archive_originals(first: Path) -> list[Path]:
    name = first.name
    m = re.search(r"^(.*)\.part0*1\.rar$", name, re.I)
    if m:
        return _matching_siblings(first, rf"^{re.escape(m.group(1))}\.part\d+\.rar$")
    m = re.search(r"^(.*)\.001$", name, re.I)
    if m:
        return _matching_siblings(first, rf"^{re.escape(m.group(1))}\.\d{{3}}$")
    if first.suffix.lower() == ".rar":
        return [first, *_matching_siblings(first, rf"^{re.escape(first.stem)}\.r\d\d$")]
    return [first]

def _check_multipart(first: Path) -> None:
    m = re.search(r"^(.*)\.part0*1\.rar$", first.name, re.I)
    if not m:
        return
    prefix = m.group(1)
    numbers = []
    for p in _matching_siblings(first, rf"^{re.escape(prefix)}\.part\d+\.rar$"):
        part = re.search(r"\.part(\d+)\.rar$", p.name, re.I)
        if part:
            numbers.append(int(part.group(1)))
    if 1 not in numbers:
        raise ProviderFailure("ARCHIVE_PART_MISSING", f"Missing multipart RAR first part near {first.name}")

def _is_rar(first: Path) -> bool:
    return first.suffix.lower() == ".rar"

def _copy_to_output(path: Path, input_dir: Path, output_dir: Path) -> Path:
    dest = output_dir / path.relative_to(input_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return dest

def _archive_output_dir(archive: Path, input_dir: Path, output_dir: Path) -> Path:
    name = archive.name
    m = re.search(r"^(.*)\.part0*1\.rar$", name, re.I)
    base = m.group(1) if m else name
    if not m:
        m = re.search(r"^(.*)\.\d{3}$", name, re.I)
        base = Path(m.group(1)).stem if m else base
    if not m:
        for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".7z", ".rar", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".iso"):
            if base.lower().endswith(suffix):
                base = base[:-len(suffix)]
                break
    return output_dir / archive.parent.relative_to(input_dir) / safe_name(base)

def _extract_cmd(tool: str, archive: Path, output_dir: Path, pw: str | None) -> list[str]:
    if tool == "unrar":
        return ["unrar", "x", "-o+", f"-p{pw}" if pw else "-p-", str(archive), str(output_dir)]
    cmd = ["7z", "x", "-y", f"-o{output_dir}", str(archive)]
    if pw:
        cmd.insert(2, f"-p{pw}")
    return cmd

def _flatten_same_name_root(extract_dir: Path) -> None:
    entries = list(extract_dir.iterdir())
    if len(entries) != 1 or not entries[0].is_dir() or entries[0].name.lower() != extract_dir.name.lower():
        return
    nested = entries[0]
    for child in nested.iterdir():
        shutil.move(str(child), str(extract_dir / child.name))
    nested.rmdir()

async def extract_archives(input_dir: Path, output_dir: Path, progress: JobState, password: str | list[str] | None = None, delete_archive: bool = False) -> list[Path]:
    if not shutil.which("7z"):
        progress.log("[SKIP] 7z not installed, uploading original files without extract.")
        output_dir.mkdir(parents=True, exist_ok=True)
        return [_copy_to_output(p, input_dir, output_dir) for p in input_dir.rglob("*") if p.is_file()]
    found = archives(input_dir)
    if not found:
        output_dir.mkdir(parents=True, exist_ok=True)
        return [_copy_to_output(p, input_dir, output_dir) for p in input_dir.rglob("*") if p.is_file()]
    output_dir.mkdir(parents=True, exist_ok=True)
    total = len(found)
    archive_originals = {p for archive in found for p in _archive_originals(archive)}
    keep_originals: list[Path] = []

    pw_candidates: list[str | None] = [None]
    if isinstance(password, list):
        for pw in password:
            if pw and pw not in pw_candidates:
                pw_candidates.append(pw)
    elif isinstance(password, str) and password:
        pw_candidates.append(password)

    for i, archive in enumerate(found, 1):
        progress.check_cancelled()
        progress.set(step="extracting", current_file=archive.name)
        extract_dir = _archive_output_dir(archive, input_dir, output_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)
        before = {p for p in extract_dir.rglob("*") if p.is_file()}

        extracted = False
        last_error_text = ""
        try:
            _check_multipart(archive)
            tools = (["unrar"] if _is_rar(archive) and shutil.which("unrar") else []) + ["7z"]
            for tool in tools:
                progress.log(f"Trying {tool}: {archive.name}")
                for pw in pw_candidates:
                    proc = await asyncio.create_subprocess_exec(*_extract_cmd(tool, archive, extract_dir, pw), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                    out, _ = await proc.communicate()
                    text = out.decode(errors="ignore")
                    if proc.returncode == 0:
                        extracted = True
                        break
                    last_error_text = text
                    lowered = text.lower()
                    if "volume" in lowered and "missing" in lowered:
                        raise ProviderFailure("ARCHIVE_PART_MISSING", text[-500:])
                if extracted:
                    break
        except ProviderFailure as exc:
            last_error_text = exc.message

        if not extracted:
            originals = _archive_originals(archive)
            keep_originals.extend(_copy_to_output(p, input_dir, output_dir) for p in originals if p.is_file())
            progress.log(f"[SKIP] Extract failed, uploading original archive: {archive.name} ({last_error_text[-160:]})")
            for p in set(extract_dir.rglob("*")) - before:
                if p.is_file() and p not in keep_originals:
                    p.unlink(missing_ok=True)
            progress.progress.extract = i / total * 100
            continue

        _flatten_same_name_root(extract_dir)
        progress.progress.extract = i / total * 100
        if delete_archive:
            for p in _archive_originals(archive):
                p.unlink(missing_ok=True)
    for p in input_dir.rglob("*"):
        if p.is_file() and p not in archive_originals:
            _copy_to_output(p, input_dir, output_dir)
    return [p for p in output_dir.rglob("*") if p.is_file()]
