from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import TestCase

from src.extract import extractor as extractor_mod
from src.extract.extractor import extract_archives, sanitize_extracted_tree
from src.jobs.progress import JobState
from src.providers.base import safe_name, strip_emoji


def test_strip_emoji_various_categories():
    # 1. Standard Emoticons & Pictographs
    assert strip_emoji("📁 My Folder 🚀") == " My Folder "
    assert strip_emoji("Ảnh 📸.jpg") == "Ảnh .jpg"
    assert strip_emoji("Movie 🎬 1080p 🌟.mp4") == "Movie  1080p .mp4"
    assert strip_emoji("Music 🎧 [FLAC] 🎵") == "Music  [FLAC] "
    assert strip_emoji("Unicorn 🦄 Robot 🤖") == "Unicorn  Robot "

    # 2. Flags & Regional Indicators
    assert strip_emoji("Vietnam 🇻🇳 Japan 🇯🇵 USA 🇺🇸") == "Vietnam  Japan  USA "

    # 3. Keycaps & Modifiers
    assert strip_emoji("Track 1️⃣ 2️⃣ #️⃣") == "Track   "

    # 4. Complex ZWJ compound emojis & Skin tones
    assert strip_emoji("Family 👨‍👩‍👧‍👦 Developer 👩‍💻 ThumbsUp 👍🏽") == "Family  Developer  ThumbsUp "

    # 5. Dingbats & Symbols
    assert strip_emoji("Sparkles ✨ Check ✔️ Star ⭐ Sun ☀️ Warning ⚠️") == "Sparkles  Check  Star  Sun  Warning "


def test_preserve_multilingual_characters():
    # Vietnamese
    vi_text = "Tiếng Việt có dấu: à á ả ã ạ â ầ ấ ẩ ẫ ậ ă ằ ắ ẳ ẵ ặ è é ẻ ẽ ẹ ê ề ế ể ễ ệ ì í ỉ ĩ ị ò ó ỏ õ ọ ô ồ ố ổ ỗ ộ ơ ờ ớ ở ỡ ợ ù ú ủ ũ ụ ư ừ ứ ử ữ ự ỳ ý ỷ ỹ ỵ đ"
    assert strip_emoji(f"{vi_text} 🚀") == f"{vi_text} "
    assert safe_name("Ảnh 📸 [2026].jpg") == "Ảnh [2026].jpg"
    assert safe_name("Ảnh_đẹp_2026_📸.jpg") == "Ảnh_đẹp_2026_.jpg"

    # Chinese Simplified & Traditional
    zh_text = "【1080P】东京喰种 第一季 简体中文 與 繁體中文 測試"
    assert strip_emoji(f"{zh_text} 🌸") == f"{zh_text} "
    assert safe_name(f"{zh_text} 🎬.mkv") == f"{zh_text}.mkv"

    # Japanese (Kanji, Hiragana, Katakana)
    ja_text = "日本語のタイトル ひらがな カタカナ 映画"
    assert strip_emoji(f"{ja_text} 🇯🇵") == f"{ja_text} "
    assert safe_name(f"{ja_text} 🌸.zip", is_dir=False) == f"{ja_text}.zip"

    # Korean (Hangul)
    ko_text = "한국어_드라마_다운로드_폴더_테스트"
    assert strip_emoji(f"{ko_text} 🦄") == f"{ko_text} "
    assert safe_name(f"{ko_text} 🌟", is_dir=True) == ko_text

    # Russian (Cyrillic)
    ru_text = "Привет мир Полный сезон"
    assert strip_emoji(f"{ru_text} 🚀") == f"{ru_text} "
    assert safe_name(f"{ru_text} 📦.rar") == f"{ru_text}.rar"

    # Thai, Arabic, German, French
    assert safe_name("ภาษาไทย 🚀.mp4") == "ภาษาไทย.mp4"
    assert safe_name("اختبار_الملف 📁.pdf") == "اختبار_الملف.pdf"
    assert safe_name("Äpfel Übermäßig 🍎.txt") == "Äpfel Übermäßig.txt"
    assert safe_name("Français été 🥐.png") == "Français été.png"


def test_safe_name_edge_cases():
    # Only emojis in filename
    assert safe_name("🎬.mp4") == "file.mp4"
    assert safe_name("🎬🚀✨.png") == "file.png"
    assert safe_name("🎬") == "file"

    # Only emojis in directory name
    assert safe_name("📁", is_dir=True) == "folder"
    assert safe_name("📁🚀🌸", is_dir=True) == "folder"

    # Emojis with illegal OS chars
    assert safe_name('📁 My "Best" <Show> 🚀: Part 1.mp4') == "My Best Show Part 1.mp4"
    assert safe_name('Folder: "Name" 📁', is_dir=True) == "Folder Name"

    # Multiple spaces collapsed
    assert safe_name("My    Folder   🚀   Name", is_dir=True) == "My Folder Name"
    assert safe_name("File   🌸   Name   🎬.mkv") == "File Name.mkv"


def test_sanitize_extracted_tree_nested(tmp_path):
    root = tmp_path / "extracted_root"
    root.mkdir()

    sub1 = root / "📁 Folder A 🚀"
    sub1.mkdir()
    sub2 = sub1 / "🌸 Sub B 🌸"
    sub2.mkdir()

    (sub2 / "Ảnh 📸 [2026].jpg").write_text("image1")
    (sub2 / "日本語 🇯🇵.txt").write_text("text1")
    (sub1 / "테스트 🦄 1️⃣.png").write_text("image2")
    (root / "🎬.mp4").write_text("video1")
    (root / "normal_file.doc").write_text("doc1")

    sanitize_extracted_tree(root)

    expected_rel_paths = {
        "Folder A/Sub B/Ảnh [2026].jpg",
        "Folder A/Sub B/日本語.txt",
        "Folder A/테스트.png",
        "file.mp4",
        "normal_file.doc",
    }

    actual_files = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    assert actual_files == expected_rel_paths


def test_sanitize_extracted_tree_collision_handling(tmp_path):
    root = tmp_path / "collision_root"
    root.mkdir()

    # Two files that would have the exact same sanitized name "file.txt"
    (root / "file 🚀.txt").write_text("content1")
    (root / "file 🌟.txt").write_text("content2")
    (root / "file.txt").write_text("content3")

    sanitize_extracted_tree(root)

    files = sorted(p.name for p in root.iterdir() if p.is_file())
    assert len(files) == 3
    assert "file.txt" in files
    assert "file_1.txt" in files
    assert "file_2.txt" in files


def test_extract_archives_sanitizes_emoji_names(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()

    archive = input_dir / "【Pack】Ảnh 🌸.zip"
    archive.write_text("zip")

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", None

    async def fake_exec(*args, **kwargs):
        out_arg = next(arg for arg in args if str(arg).startswith("-o"))
        target = Path(str(out_arg)[2:])
        target.mkdir(parents=True, exist_ok=True)
        # 7z extracts files with emoji names inside target dir
        sub = target / "📁 Nested 🚀"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "Photo 📸.jpg").write_text("jpg-bytes")
        (target / "🎬 Video 1️⃣.mp4").write_text("mp4-bytes")
        return Proc()

    monkeypatch.setattr(extractor_mod.shutil, "which", lambda name: "7z")
    monkeypatch.setattr(extractor_mod.asyncio, "create_subprocess_exec", fake_exec)

    job = JobState("emoji-extract-job", {})
    outputs = asyncio.run(extract_archives(input_dir, output_dir, job, None))

    actual_rel_paths = {p.relative_to(output_dir).as_posix() for p in outputs}
    expected_rel_paths = {
        "【Pack】Ảnh/Nested/Photo.jpg",
        "【Pack】Ảnh/Video.mp4",
    }

    assert actual_rel_paths == expected_rel_paths
    assert not any(p.name.endswith(".zip") for p in outputs)
    assert all(not strip_emoji(p.name) != p.name for p in outputs)
