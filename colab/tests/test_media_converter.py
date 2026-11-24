from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

_COLAB_DIR = Path(__file__).resolve().parent.parent
if str(_COLAB_DIR) not in sys.path:
    sys.path.insert(0, str(_COLAB_DIR))

from PIL import Image

from src.utils.media_converter import (
    convert_directory,
    convert_image_file,
    detect_file_category,
    extract_item_ext,
    is_auto_convert_enabled,
    is_file_filter_enabled,
    parse_auto_convert_config,
    parse_file_filter,
    resolve_conversion_decision,
    resolve_conversion_target,
    should_download_file_item,
    validate_converted_image,
)


class MockJobState:
    def __init__(self) -> None:
        self.logs: list[str] = []
        self.current_file: str = ""
        self.converted_files: list[dict] = []
        self.progress = SimpleNamespace(convert=0)

    def set(self, **kwargs) -> None:
        if "current_file" in kwargs:
            self.current_file = kwargs["current_file"]

    def log(self, message: str) -> None:
        self.logs.append(message)

    def check_cancelled(self) -> None:
        pass


class MediaConverterAndFilterTests(TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.src_dir = Path(self.temp_dir) / "src"
        self.dest_dir = Path(self.temp_dir) / "dest"
        self.src_dir.mkdir(parents=True, exist_ok=True)
        self.dest_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_extract_item_ext_and_category_detection(self) -> None:
        raw_ext, norm_ext = extract_item_ext("photo.JPEG")
        self.assertEqual(raw_ext, "jpeg")
        self.assertEqual(norm_ext, "jpg")

        _, norm_vid = extract_item_ext("video.MKV")
        self.assertEqual(norm_vid, "mkv")

        _, norm_pdf = extract_item_ext({"name": "document.PDF"})
        self.assertEqual(norm_pdf, "pdf")

        _, norm_7z = extract_item_ext({"path": "folder/archive.7z"})
        self.assertEqual(norm_7z, "7z")

        self.assertEqual(detect_file_category("clip.mp4"), "video")
        self.assertEqual(detect_file_category("song.flac"), "audio")
        self.assertEqual(detect_file_category("doc.docx"), "document")
        self.assertEqual(detect_file_category("pack.tar.gz"), "archive")
        self.assertEqual(detect_file_category("picture.webp"), "image")
        self.assertEqual(detect_file_category("unknown.xyz123"), "other")

    def test_file_filter_logic(self) -> None:
        # 1. Disabled or default "all"
        self.assertFalse(is_file_filter_enabled(None))
        self.assertFalse(is_file_filter_enabled({"file_filter": {"enabled": False}}))
        self.assertTrue(should_download_file_item("clip.mp4", {}))

        # 2. Exclude/except mode (e.g. download all files except mp4)
        except_mp4_opts = {
            "file_filter": {
                "enabled": True,
                "mode": "except",
                "category": "all",
                "formats": ["mp4"],
            }
        }
        self.assertTrue(is_file_filter_enabled(except_mp4_opts))
        # MP4 should NOT be downloaded
        self.assertFalse(should_download_file_item("movie.mp4", except_mp4_opts))
        self.assertFalse(should_download_file_item({"name": "movie.MP4"}, except_mp4_opts))
        # Other formats SHOULD be downloaded
        self.assertTrue(should_download_file_item("clip.mkv", except_mp4_opts))
        self.assertTrue(should_download_file_item("photo.jpg", except_mp4_opts))
        self.assertTrue(should_download_file_item("doc.pdf", except_mp4_opts))

        # 3. Include mode by specific formats
        include_img_opts = {
            "file_filter": {
                "enabled": True,
                "mode": "include",
                "category": "all",
                "formats": ["png", "webp"],
            }
        }
        self.assertTrue(should_download_file_item("logo.png", include_img_opts))
        self.assertTrue(should_download_file_item("banner.webp", include_img_opts))
        self.assertFalse(should_download_file_item("photo.jpg", include_img_opts))
        self.assertFalse(should_download_file_item("video.mp4", include_img_opts))

        # 4. Include mode by category (video only)
        category_video_opts = {
            "file_filter": {
                "enabled": True,
                "mode": "include",
                "category": "video",
                "formats": [],
            }
        }
        self.assertTrue(should_download_file_item("movie.mkv", category_video_opts))
        self.assertTrue(should_download_file_item("movie.mp4", category_video_opts))
        self.assertFalse(should_download_file_item("photo.jpg", category_video_opts))

    def test_auto_convert_decision_and_skip_when_already_target(self) -> None:
        opts = {
            "auto_convert": {
                "enabled": True,
                "video": {
                    "enabled": True,
                    "input_mode": "all",
                    "input_formats": [],
                    "output_formats": ["mp4"],
                    "target_format": "mp4",
                },
                "image": {
                    "enabled": True,
                    "input_mode": "include",
                    "input_formats": ["webp", "bmp"],
                    "output_formats": ["jpg", "png"],
                    "target_format": "jpg",
                },
            }
        }
        self.assertTrue(is_auto_convert_enabled(opts))

        # Video: MKV -> convert to MP4
        cat_mkv, should_mkv, target_mkv, reason_mkv = resolve_conversion_decision("movie.mkv", opts)
        self.assertTrue(should_mkv)
        self.assertEqual(target_mkv, "mp4")
        self.assertEqual(cat_mkv, "video")

        # Video: MP4 -> already in accepted output_formats -> SKIP
        cat_mp4, should_mp4, target_mp4, reason_mp4 = resolve_conversion_decision("movie.mp4", opts)
        self.assertFalse(should_mp4)
        self.assertEqual(reason_mp4, "already_accepted:mp4")

        # Image: WEBP is in input_formats ["webp", "bmp"] -> convert to JPG
        cat_webp, should_webp, target_webp, reason_webp = resolve_conversion_decision("photo.webp", opts)
        self.assertTrue(should_webp)
        self.assertEqual(target_webp, "jpg")

        # Image: JPG is already in accepted output_formats ["jpg", "png"] -> SKIP
        cat_jpg, should_jpg, target_jpg, reason_jpg = resolve_conversion_decision("photo.jpg", opts)
        self.assertFalse(should_jpg)
        self.assertEqual(reason_jpg, "already_accepted:jpg")

        # Image: GIF is NOT in include list ["webp", "bmp"] -> SKIP
        cat_gif, should_gif, target_gif, reason_gif = resolve_conversion_decision("anim.gif", opts)
        self.assertFalse(should_gif)
        self.assertEqual(reason_gif, "not_included:gif")

        # Check resolve_conversion_target preview
        self.assertEqual(resolve_conversion_target("movie.mkv", opts), "mp4")
        self.assertIsNone(resolve_conversion_target("movie.mp4", opts))

    def test_auto_convert_except_mode(self) -> None:
        opts = {
            "auto_convert": {
                "enabled": True,
                "video": {
                    "enabled": True,
                    "input_mode": "except",
                    "input_formats": ["mov"],
                    "output_formats": ["mp4"],
                    "target_format": "mp4",
                },
            }
        }
        # MKV is not in except list -> convert to MP4
        cat_mkv, should_mkv, target_mkv, reason_mkv = resolve_conversion_decision("clip.mkv", opts)
        self.assertTrue(should_mkv)
        self.assertEqual(target_mkv, "mp4")

        # MOV is in except list -> skip
        cat_mov, should_mov, target_mov, reason_mov = resolve_conversion_decision("clip.mov", opts)
        self.assertFalse(should_mov)
        self.assertEqual(reason_mov, "excluded:mov")

    def test_auto_convert_defaults_video_to_mp4_except_gif_and_image_to_jpg(self) -> None:
        # Default options when video and image conversion are enabled without custom filters
        opts = {
            "auto_convert": {
                "enabled": True,
                "video": {
                    "enabled": True,
                },
                "image": {
                    "enabled": True,
                },
            }
        }
        cfg = parse_auto_convert_config(opts)
        # Check Video default rule: mode except, input_formats ['gif'], output_formats ['mp4'], target 'mp4'
        self.assertEqual(cfg["video"].input_mode, "except")
        self.assertIn("gif", cfg["video"].input_formats)
        self.assertEqual(cfg["video"].output_formats, ["mp4"])
        self.assertEqual(cfg["video"].target_format, "mp4")

        # Check Image default rule: mode all, output_formats ['jpg'], target 'jpg'
        self.assertEqual(cfg["image"].input_mode, "all")
        self.assertEqual(cfg["image"].output_formats, ["jpg"])
        self.assertEqual(cfg["image"].target_format, "jpg")

        # Test video file (MKV) converts to MP4
        cat_mkv, should_mkv, target_mkv, _ = resolve_conversion_decision("sample.mkv", opts)
        self.assertEqual(cat_mkv, "video")
        self.assertTrue(should_mkv)
        self.assertEqual(target_mkv, "mp4")

        # Test video file already MP4 is skipped
        cat_mp4, should_mp4, target_mp4, reason_mp4 = resolve_conversion_decision("sample.mp4", opts)
        self.assertEqual(cat_mp4, "video")
        self.assertFalse(should_mp4)
        self.assertEqual(reason_mp4, "already_accepted:mp4")

        # Test GIF file is NOT converted by video conversion (even if only video conversion enabled)
        vid_only_opts = {"auto_convert": {"enabled": True, "video": {"enabled": True}}}
        cat_gif, should_gif, target_gif, _ = resolve_conversion_decision("anim.gif", vid_only_opts)
        self.assertFalse(should_gif)
        self.assertIsNone(target_gif)

        # Test Image file (PNG) converts to JPG
        cat_png, should_png, target_png, _ = resolve_conversion_decision("photo.png", opts)
        self.assertEqual(cat_png, "image")
        self.assertTrue(should_png)
        self.assertEqual(target_png, "jpg")

    def test_validate_converted_image_and_corruption_detection(self) -> None:
        valid_img_path = self.src_dir / "valid.png"
        img = Image.new("RGB", (100, 100), color="red")
        img.save(valid_img_path, format="PNG")

        ok_valid, msg_valid = validate_converted_image(valid_img_path, valid_img_path, "png")
        self.assertTrue(ok_valid)
        self.assertEqual(msg_valid, "ok")

        corrupt_img_path = self.src_dir / "corrupt.png"
        corrupt_img_path.write_bytes(b"not an image file content")
        ok_corrupt, msg_corrupt = validate_converted_image(valid_img_path, corrupt_img_path, "png")
        self.assertFalse(ok_corrupt)
        self.assertIn("failed", msg_corrupt.lower())

        zero_img_path = self.src_dir / "empty.png"
        zero_img_path.write_bytes(b"")
        ok_zero, msg_zero = validate_converted_image(valid_img_path, zero_img_path, "png")
        self.assertFalse(ok_zero)
        self.assertIn("empty", msg_zero.lower())

    def test_convert_image_file_preserves_original(self) -> None:
        src_png = self.src_dir / "test_orig.png"
        img = Image.new("RGB", (200, 200), color="green")
        img.save(src_png, format="PNG")
        orig_bytes = src_png.read_bytes()

        dest_jpg = self.dest_dir / "test_orig.jpg"
        ok, msg = convert_image_file(src_png, dest_jpg, "jpg")
        self.assertTrue(ok)

        # 1. Output file exists and is valid
        self.assertTrue(dest_jpg.exists())
        self.assertEqual(dest_jpg.suffix.lower(), ".jpg")
        with Image.open(dest_jpg) as res_img:
            self.assertEqual(res_img.size, (200, 200))

        # 2. Original source file remains completely intact and unchanged
        self.assertTrue(src_png.exists())
        self.assertEqual(src_png.read_bytes(), orig_bytes)

    def test_convert_directory_batch_processing(self) -> None:
        # Create test files
        img_png = self.src_dir / "sample1.png"
        Image.new("RGB", (50, 50), color="blue").save(img_png, format="PNG")

        img_jpg = self.src_dir / "sample2.jpg"
        Image.new("RGB", (60, 60), color="yellow").save(img_jpg, format="JPEG")

        text_file = self.src_dir / "notes.txt"
        text_file.write_text("keep me as is", encoding="utf-8")

        opts = {
            "auto_convert": {
                "enabled": True,
                "image": {
                    "enabled": True,
                    "input_mode": "all",
                    "input_formats": [],
                    "output_formats": ["jpg"],
                    "target_format": "jpg",
                },
            }
        }

        job = MockJobState()
        summary = convert_directory(self.src_dir, self.dest_dir, opts, job)

        # 1. Original directory files are intact
        self.assertTrue(img_png.exists())
        self.assertTrue(img_jpg.exists())
        self.assertTrue(text_file.exists())

        # 2. Converted png -> jpg in destination
        dest_jpg1 = self.dest_dir / "sample1.jpg"
        self.assertTrue(dest_jpg1.exists())

        # 3. sample2.jpg already in output_formats -> copied as is without re-converting
        dest_jpg2 = self.dest_dir / "sample2.jpg"
        self.assertTrue(dest_jpg2.exists())

        # 4. notes.txt copied to dest intact
        dest_txt = self.dest_dir / "notes.txt"
        self.assertTrue(dest_txt.exists())
        self.assertEqual(dest_txt.read_text(encoding="utf-8"), "keep me as is")

        # 5. Check summary output
        self.assertEqual(len(summary["converted"]), 1)
        self.assertEqual(summary["converted"][0]["target_format"], "jpg")
        self.assertEqual(len(summary["failed"]), 0)
