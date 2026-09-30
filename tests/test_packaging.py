"""Package contracts: no local dependencies/private files, safe modes and reproducibility."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from app_info import APP_ID, VERSION, ICON_ROOT

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_deb", ROOT / "packaging/build_deb.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1700000000"}):
            cls.artifact = builder.build(Path(cls.directory.name) / "first")
        data = subprocess.check_output(["dpkg-deb", "--fsys-tarfile", str(cls.artifact)])
        cls.archive = tarfile.open(fileobj=io.BytesIO(data))

    @classmethod
    def tearDownClass(cls):
        cls.archive.close()
        cls.directory.cleanup()

    def test_control_and_system_dependencies(self):
        metadata = subprocess.check_output(["dpkg-deb", "--field", str(self.artifact)], text=True)
        self.assertIn(f"Version: {VERSION}\n", metadata)
        self.assertIn("Architecture: all\n", metadata)
        for dependency in (
            "python3-gi-cairo",
            "gir1.2-gtk-4.0 (>= 4.10)",
            "gir1.2-ayatanaappindicator3-0.1",
        ):
            self.assertIn(dependency, metadata)

    def test_archive_modes_ownership_and_allowlist(self):
        members = self.archive.getmembers()
        names = {m.name for m in members}
        self.assertIn("./usr/bin/shotmark", names)
        self.assertIn(f"./usr/share/applications/{APP_ID}.desktop", names)
        self.assertIn(f"./usr/share/icons/hicolor/scalable/apps/{APP_ID}-symbolic.svg", names)
        self.assertNotIn("./usr/share/icons/hicolor/index.theme", names)
        forbidden = (
            ".deps",
            "__pycache__",
            ".git",
            ".review-backups",
            "autostart",
            "home",
            "tests",
        )
        for member in members:
            self.assertEqual((member.uid, member.gid), (0, 0), member.name)
            self.assertFalse(
                any(part in forbidden for part in Path(member.name).parts), member.name
            )
            if member.isfile():
                expected = 0o755 if member.name == "./usr/bin/shotmark" else 0o644
                self.assertEqual(member.mode, expected, member.name)
        launcher = self.archive.extractfile("./usr/bin/shotmark").read().decode()
        self.assertIn("/usr/share/shotmark/tray.py", launcher)
        self.assertNotIn(str(ROOT), launcher)

    def test_desktop_entry_validates(self):
        content = self.archive.extractfile(f"./usr/share/applications/{APP_ID}.desktop").read()
        path = Path(self.directory.name) / "check.desktop"
        path.write_bytes(content)
        result = subprocess.run(
            ["desktop-file-validate", str(path)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Icon={APP_ID}".encode(), content)

    def test_reproducible_package_and_checksum(self):
        with patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1700000000"}):
            again = builder.build(Path(self.directory.name) / "second")
        digest = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        self.assertEqual(digest, hashlib.sha256(again.read_bytes()).hexdigest())
        self.assertEqual((again.parent / "SHA256SUMS").read_text(), f"{digest}  {again.name}\n")

    def test_icon_sizes_and_transparency(self):
        for size in (16, 24, 32, 48, 64, 128, 256, 512):
            with Image.open(ICON_ROOT / f"{size}x{size}/apps/{APP_ID}.png") as icon:
                self.assertEqual(icon.size, (size, size))
                self.assertEqual(icon.mode, "RGBA")
                self.assertEqual(icon.getchannel("A").getextrema()[0], 0)


if __name__ == "__main__":
    unittest.main()
