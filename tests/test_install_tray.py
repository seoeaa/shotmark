"""Установка в временный prefix; GTK3-трей проверяется отдельным процессом."""
from pathlib import Path
import subprocess
import select
import uuid
import sys
import tempfile
import unittest
from unittest.mock import patch

import install_user
from app_info import APP_ID, VERSION


class InstallTests(unittest.TestCase):
    def test_install_copy_launcher_desktop_and_uninstall(self):
        with tempfile.TemporaryDirectory(prefix="shotmark install ") as directory:
            prefix = Path(directory)
            package, launcher, desktop, backup = install_user.install(prefix)
            self.assertIsNone(backup)
            self.assertTrue((package / "tray.py").is_file())
            self.assertTrue((package / ".shotmark-managed").is_file())
            version = subprocess.run(
                [str(launcher), "--version"], capture_output=True, text=True, timeout=5
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertIn(f"shotmark {VERSION}", version.stdout)
            icon = package / f"assets/icons/hicolor/256x256/apps/{APP_ID}.png"
            self.assertTrue(icon.is_file())
            self.assertIn(f"Icon={icon}", desktop.read_text())
            validation = subprocess.run(
                ["desktop-file-validate", str(desktop)], capture_output=True, text=True
            )
            self.assertEqual(validation.returncode, 0, validation.stderr)
            self.assertFalse((prefix / "config/autostart").exists())
            # В тесте не посылаем quit настоящему пользовательскому трею.
            with patch.object(install_user.subprocess, "run"):
                install_user.uninstall(prefix)
            self.assertFalse(package.exists())
            self.assertFalse(launcher.exists())
            self.assertFalse(desktop.exists())

    def test_upgrade_preserves_previous_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            package, *_ = install_user.install(directory)
            (package / "local-note.txt").write_text("keep me")
            _, _, _, backup = install_user.install(directory)
            self.assertEqual((backup / "local-note.txt").read_text(), "keep me")

    def test_foreign_directory_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            package, _, _ = install_user.locations(directory)
            package.mkdir(parents=True)
            precious = package / "user.txt"
            precious.write_text("user data")
            with self.assertRaises(RuntimeError):
                install_user.install(directory)
            self.assertEqual(precious.read_text(), "user data")

    def test_foreign_launcher_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            _, launcher, _ = install_user.locations(directory)
            launcher.parent.mkdir(parents=True)
            launcher.write_text("user custom launcher")
            with self.assertRaises(RuntimeError):
                install_user.install(directory)
            self.assertEqual(launcher.read_text(), "user custom launcher")


class TrayTests(unittest.TestCase):
    def test_remote_quit_and_missing_instance_noop(self):
        identity = "io.github.shotmark.Test" + uuid.uuid4().hex
        setup = f"import tray; tray.TRAY_ID={identity!r}; "
        owner_code = setup + "tray.watcher_available=lambda:True; raise SystemExit(tray.main([]))"
        owner = subprocess.Popen(
            [sys.executable, "-c", owner_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready, _, _ = select.select([owner.stdout], [], [], 3)
            self.assertTrue(ready, "tray did not start")
            self.assertIn("трей запущен", owner.stdout.readline())
            command = [sys.executable, "-c", setup + 'raise SystemExit(tray.main(["--quit"]))']
            result = subprocess.run(command, capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("did not unregister", result.stderr)
            owner.wait(timeout=3)
            self.assertEqual(owner.returncode, 0)
            absent = subprocess.run(command, capture_output=True, text=True, timeout=3)
            self.assertEqual(absent.returncode, 0, absent.stderr)
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.communicate()

    def test_tray_gtk3_menu_and_editor_child_lifecycle(self):
        code = """
import sys
from unittest.mock import patch
import gi
import tray
from gi.repository import GLib, Gtk
assert gi.get_required_version("Gtk") == "3.0"
assert "editor" not in sys.modules
assert tray.editor_command(["--file", "/tmp/image with spaces.png"])[-1] == "/tmp/image with spaces.png"
app = tray.TrayApp()
app.register(None)
with patch.object(tray, "watcher_available", return_value=True):
    app.activate()
    first = app.indicator
    app.activate()
    assert app.indicator is first
assert [item.get_label() for item in app.capture_items] == [
    "Сделать скриншот", "Системный выбор области / окна", "Открыть PNG…"]
errors = []
app._message = lambda *args, **kwargs: errors.append(args)
def run_child(status):
    with patch.object(tray, "editor_command", return_value=[sys.executable, "-c", f"raise SystemExit({status})"]):
        app._start_editor()
    assert not app.capture_items[0].get_sensitive()
    loop = GLib.MainLoop()
    def wait():
        if app.editor_process is None:
            loop.quit()
            return False
        return True
    GLib.timeout_add(10, wait)
    loop.run()
    assert app.capture_items[0].get_sensitive()
run_child(0)
assert not errors
run_child(2)
assert len(errors) == 1
app.indicator.set_status(tray.AppIndicator.IndicatorStatus.PASSIVE)
print("TRAY_TEST_OK")
"""
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=8
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TRAY_TEST_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
