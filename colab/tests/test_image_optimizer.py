from __future__ import annotations

import tempfile
import shutil
from pathlib import Path
from unittest import TestCase
from PIL import Image
from src.utils.image_optimizer import optimize_directory

class MockJobState:
    def __init__(self) -> None:
        self.logs: list[str] = []
        self.current_file: str = ""

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
            "max_target_mb": 0.2,  # ~200 KB
            "start_quality": 95,
            "quality": 85,
            "auto_size": True,
            "resolution_scale": 1.0
        }

        results = optimize_directory(self.src_dir, self.dest_dir, options, job)

        # Verify non-image copied as-is
        self.assertTrue((self.dest_dir / "test.txt").exists())
        self.assertEqual((self.dest_dir / "test.txt").read_text(encoding="utf-8"), "Hello World")

        # Verify images processed and sorted by size (small.jpg is smaller, so it's first)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["name"], "small.jpg")
        self.assertEqual(results[1]["name"], "large.jpg")

        # Verify large image output is copied/created
        large_dest = self.dest_dir / "large.jpg"
        self.assertTrue(large_dest.exists())
        self.assertLessEqual(large_dest.stat().st_size, 0.2 * 1024 * 1024)
