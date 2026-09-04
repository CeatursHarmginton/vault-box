from __future__ import annotations

import tempfile
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from PIL import Image
from src.utils import image_optimizer
from src.utils.image_optimizer import optimize_directory

class MockJobState:
    def __init__(self) -> None:
        self.logs: list[str] = []
        self.current_file: str = ""
        self.progress = SimpleNamespace(optimize=0)
        self.updated_at = 0.0

    def set(self, **kwargs) -> None:
        if "current_file" in kwargs:
            self.current_file = kwargs["current_file"]

    def log(self, message: str) -> None:
        self.logs.append(message)

class ImageOptimizerTests(TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.src_dir = Path(self.temp_dir) / "src"
        self.dest_dir = Path(self.temp_dir) / "dest"
        self.src_dir.mkdir()
        self.dest_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir)

    def test_optimize_directory_handles_non_images_and_images(self) -> None:
        # 1. Create a non-image file
        non_img = self.src_dir / "test.txt"
        non_img.write_text("Hello World", encoding="utf-8")
        video = self.src_dir / "clip.mp4"
        video.write_bytes(b"video")

        # 2. Create images (large & small)
        img_large_path = self.src_dir / "large.jpg"
        img = Image.new("RGB", (2000, 2000), color="blue")
        img.save(img_large_path, "JPEG", quality=100) # Save as large file
        
        img_small_path = self.src_dir / "small.jpg"
        img_small = Image.new("RGB", (10, 10), color="red")
        img_small.save(img_small_path, "JPEG", quality=50) # Save as small file

        job = MockJobState()
        # Set max target to 0.1 MB (~100 KB) to trigger quality reduction loop
        options = {
            "optimize_image": True,
            "min_target_mb": 0.05, # ~50 KB
            "max_target_mb": 0.001,
            "start_quality": 95,
            "quality": 85,
            "auto_size": True,
            "resolution_scale": 1.0
        }

        results = optimize_directory(self.src_dir, self.dest_dir, options, job)

        # Verify non-image copied as-is
        self.assertTrue((self.dest_dir / "test.txt").exists())
        self.assertEqual((self.dest_dir / "test.txt").read_text(encoding="utf-8"), "Hello World")
        self.assertTrue((self.dest_dir / "clip.mp4").exists())

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "large.jpg")
        self.assertTrue((self.dest_dir / "small.jpg").exists())

        # Verify large image output is copied/created
        large_dest = self.dest_dir / "large.jpg"
        self.assertTrue(large_dest.exists())

    def test_optimize_directory_skips_images_under_downscale_target(self) -> None:
        img_path = self.src_dir / "small.jpg"
        Image.new("RGB", (10, 10), color="red").save(img_path, "JPEG", quality=50)

        results = optimize_directory(self.src_dir, self.dest_dir, {"max_target_mb": 3.0}, MockJobState())

        self.assertEqual(results, [])
        self.assertTrue((self.dest_dir / "small.jpg").exists())

    def test_optimize_directory_uses_worker_override(self) -> None:
        for i in range(4):
            (self.src_dir / f"{i}.jpg").write_bytes(b"x")

        seen: set[int] = set()
        old = image_optimizer.optimize_image_file
        def fake_optimize(src_path, dest_dir, options, adaptive_quality):
            seen.add(threading.get_ident())
            time.sleep(0.02)
            dest_dir.mkdir(parents=True, exist_ok=True)
            out = dest_dir / src_path.name
            out.write_bytes(b"x")
            return out, adaptive_quality, "ok"
        image_optimizer.optimize_image_file = fake_optimize
        try:
            results = optimize_directory(self.src_dir, self.dest_dir, {"optimize_workers": 4, "auto_size": False, "max_target_mb": 0.0}, MockJobState())
        finally:
            image_optimizer.optimize_image_file = old

        self.assertEqual([r["name"] for r in results], ["0.jpg", "1.jpg", "2.jpg", "3.jpg"])
        self.assertGreater(len(seen), 1)

    def test_optimize_directory_updates_optimize_progress(self) -> None:
        for i in range(3):
            (self.src_dir / f"{i}.jpg").write_bytes(b"x")

        old = image_optimizer.optimize_image_file
        def fake_optimize(src_path, dest_dir, options, adaptive_quality):
            dest_dir.mkdir(parents=True, exist_ok=True)
            out = dest_dir / src_path.name
            out.write_bytes(b"x")
            return out, adaptive_quality, "ok"
        image_optimizer.optimize_image_file = fake_optimize
        try:
            job = MockJobState()
            optimize_directory(self.src_dir, self.dest_dir, {"optimize_workers": 1, "max_target_mb": 0.0}, job)
        finally:
            image_optimizer.optimize_image_file = old

        self.assertEqual(job.progress.optimize, 100)

    def test_auto_size_worker_override_still_resets_quality_per_folder(self) -> None:
        for rel in ("a/1.jpg", "a/2.jpg", "b/1.jpg"):
            path = self.src_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 20)

        seen: list[tuple[str, int]] = []
        threads: set[int] = set()
        old = image_optimizer.optimize_image_file
        def fake_optimize(src_path, dest_dir, options, adaptive_quality):
            threads.add(threading.get_ident())
            seen.append((src_path.relative_to(self.src_dir).as_posix(), adaptive_quality))
            time.sleep(0.02)
            dest_dir.mkdir(parents=True, exist_ok=True)
            out = dest_dir / src_path.name
            out.write_bytes(b"x" * 9)
            return out, 80 if src_path.name == "1.jpg" else adaptive_quality, "ok"
        image_optimizer.optimize_image_file = fake_optimize
        try:
            optimize_directory(self.src_dir, self.dest_dir, {
                "optimize_workers": 4,
                "auto_size": True,
                "start_quality": 95,
                "max_target_mb": 0.00001,
            }, MockJobState())
        finally:
            image_optimizer.optimize_image_file = old

        self.assertEqual(sorted(seen), [("a/1.jpg", 95), ("a/2.jpg", 85), ("b/1.jpg", 95)])
        self.assertGreater(len(threads), 1)

    def test_png_is_written_as_real_jpeg(self) -> None:
        src = self.src_dir / "photo.png"
        Image.new("RGBA", (20, 20), color=(255, 0, 0, 128)).save(src, "PNG")

        results = optimize_directory(self.src_dir, self.dest_dir, {
            "min_target_mb": 0.0,
            "max_target_mb": 0.0,
            "quality": 85,
            "optimize_workers": 1,
        }, MockJobState())

        out = self.dest_dir / "photo.jpg"
        self.assertEqual(results[0]["name"], "photo.jpg")
        self.assertTrue(out.exists())
        with Image.open(out) as img:
            self.assertEqual(img.format, "JPEG")

    def test_invalid_tiny_compressed_output_is_not_selected(self) -> None:
        src = self.src_dir / "large.jpg"
        Image.new("RGB", (1600, 1600), color="blue").save(src, "JPEG", quality=100)

        old = image_optimizer.compress_image
        def fake_compress(src_path, dest_path, q, scale=1.0):
            dest_path.write_bytes(b"x" * 114)
            return True
        image_optimizer.compress_image = fake_compress
        try:
            results = optimize_directory(self.src_dir, self.dest_dir, {
                "min_target_mb": 0.0,
                "max_target_mb": 0.0,
                "start_quality": 95,
                "auto_size": True,
                "optimize_workers": 1,
            }, MockJobState())
        finally:
            image_optimizer.compress_image = old

        out = self.dest_dir / "large.jpg"
        self.assertEqual(results[0]["status"], "Giữ nguyên (Lỗi nén)")
        self.assertGreater(out.stat().st_size, 114)
        with Image.open(out) as img:
            self.assertEqual(img.size, (1600, 1600))

    def test_invalid_tiny_conversion_falls_back_to_original_file(self) -> None:
        src = self.src_dir / "small.png"
        Image.new("RGB", (20, 20), color="blue").save(src, "PNG")

        old = image_optimizer.compress_image
        def fake_compress(src_path, dest_path, q, scale=1.0):
            dest_path.write_bytes(b"x" * 114)
            return True
        image_optimizer.compress_image = fake_compress
        try:
            results = optimize_directory(self.src_dir, self.dest_dir, {
                "min_target_mb": 1.0,
                "max_target_mb": 0.0,
                "quality": 85,
                "optimize_workers": 1,
            }, MockJobState())
        finally:
            image_optimizer.compress_image = old

        out = self.dest_dir / "small.png"
        self.assertEqual(results[0]["name"], "small.png")
        self.assertTrue(out.exists())
        self.assertEqual(out.read_bytes(), src.read_bytes())
        with Image.open(out) as img:
            self.assertEqual(img.format, "PNG")

    def test_auto_size_processes_folder_images_small_to_large(self) -> None:
        for name, size in (("large.jpg", 30), ("small.jpg", 10), ("mid.jpg", 20)):
            (self.src_dir / name).write_bytes(b"x" * size)

        seen: list[str] = []
        old = image_optimizer.optimize_image_file
        def fake_optimize(src_path, dest_dir, options, adaptive_quality):
            seen.append(src_path.name)
            dest_dir.mkdir(parents=True, exist_ok=True)
            out = dest_dir / src_path.name
            out.write_bytes(b"x" * 9)
            return out, adaptive_quality, "ok"
        image_optimizer.optimize_image_file = fake_optimize
        try:
            optimize_directory(self.src_dir, self.dest_dir, {
                "max_target_mb": 0.0,
                "auto_size": True,
                "optimize_workers": 1,
            }, MockJobState())
        finally:
            image_optimizer.optimize_image_file = old

        self.assertEqual(seen, ["small.jpg", "mid.jpg", "large.jpg"])

    def test_quality_descent_stops_at_min_quality(self) -> None:
        src = self.src_dir / "big.jpg"
        Image.new("RGB", (600, 600), color="green").save(src, "JPEG", quality=100)

        tried: list[int] = []
        old = image_optimizer.compress_image
        def fake_compress(src_path, dest_path, q, scale=1.0):
            tried.append(q)
            # Never small enough: forces the loop to walk all the way down.
            return old(src_path, dest_path, q, scale)
        image_optimizer.compress_image = fake_compress
        try:
            results = optimize_directory(self.src_dir, self.dest_dir, {
                "min_target_mb": 0.0,
                "max_target_mb": 0.000001,
                "start_quality": 95,
                "auto_size": True,
                "optimize_workers": 1,
            }, MockJobState())
        finally:
            image_optimizer.compress_image = old

        self.assertTrue(tried)
        self.assertEqual(min(tried), image_optimizer.MIN_QUALITY)
        self.assertGreaterEqual(results[0]["quality"], image_optimizer.MIN_QUALITY)

    def test_quality_options_below_min_are_raised_to_min(self) -> None:
        self.assertEqual(image_optimizer.clamp_quality(10), 65)
        self.assertEqual(image_optimizer.clamp_quality(0), 65)
        self.assertEqual(image_optimizer.clamp_quality(-5), 65)
        self.assertEqual(image_optimizer.clamp_quality("40"), 65)
        self.assertEqual(image_optimizer.clamp_quality(None), 95)
        self.assertEqual(image_optimizer.clamp_quality(80), 80)
        self.assertEqual(image_optimizer.clamp_quality(140), 100)

        src = self.src_dir / "tiny.png"
        Image.new("RGB", (30, 30), color="blue").save(src, "PNG")
        used: list[int] = []
        old = image_optimizer.compress_image
        def spy(src_path, dest_path, q, scale=1.0):
            used.append(q)
            return old(src_path, dest_path, q, scale)
        image_optimizer.compress_image = spy
        try:
            optimize_directory(self.src_dir, self.dest_dir, {
                "min_target_mb": 0.0,
                "max_target_mb": 0.0,
                "quality": 20,
                "start_quality": 30,
                "auto_size": False,
                "optimize_workers": 1,
            }, MockJobState())
        finally:
            image_optimizer.compress_image = old

        self.assertTrue(used)
        self.assertTrue(all(q >= 65 for q in used), used)

    def test_optimizer_ignores_its_own_output_inside_the_input_tree(self) -> None:
        (self.src_dir / "keep.txt").write_text("ok")
        nested_out = self.src_dir / "optimized"
        results = optimize_directory(self.src_dir, nested_out, {
            "min_target_mb": 0.0,
            "max_target_mb": 1.0,
            "optimize_workers": 1,
        }, MockJobState())

        self.assertEqual(results, [])
        self.assertTrue((nested_out / "keep.txt").exists())
        self.assertFalse((nested_out / "optimized").exists())
