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
