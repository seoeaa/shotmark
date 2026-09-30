"""Детерминированные проверки: только синтетические PNG и fake-портал.

Запуск: dbus-run-session -- xvfb-run -a env GDK_BACKEND=x11 \
    /usr/bin/python3 -m unittest discover -s tests -v
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from cairo_bootstrap import import_gi

gi = import_gi()
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk
import cairo
import editor
import portal
import shotmark
from storage import copy_capture, save_png


def image(width=120, height=80):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1, 1, 1)
    ctx.paint()
    return surface


def dispatch(callback):
    source = GLib.idle_source_new()
    source.set_callback(lambda *_: (callback(), GLib.SOURCE_REMOVE)[1])
    source.attach(GLib.MainContext.get_thread_default() or GLib.MainContext.default())


class FakeBus:
    def __init__(self, *, uri=None, response=0, silent=False, fail=False):
        self.uri, self.response, self.silent, self.fail = uri, response, silent, fail
        self.subscriptions = {}
        self.serial = 0
        self.closed = []
        self.call_timeout = None

    def get_unique_name(self):
        return ":1.42"

    def signal_subscribe(self, sender, iface, signal, path, arg, flags, callback, data):
        self.serial += 1
        self.subscriptions[self.serial] = (path, callback)
        return self.serial

    def signal_unsubscribe(self, number):
        self.subscriptions.pop(number, None)

    def call(
        self,
        name,
        path,
        interface,
        method,
        params,
        reply_type,
        flags,
        timeout,
        cancellable,
        callback,
        data,
    ):
        if method == "Close":
            self.closed.append(path)
            return
        self.call_timeout = timeout
        self.handle = next(iter(self.subscriptions.values()))[0]
        if self.silent:
            return

        def reply():
            callback(self, None, data)
            if not self.fail:
                payload = {} if self.uri is None else {"uri": GLib.Variant("s", self.uri)}
                for subpath, handler in list(self.subscriptions.values()):
                    handler(
                        self,
                        name,
                        subpath,
                        interface,
                        "Response",
                        GLib.Variant("(ua{sv})", (self.response, payload)),
                        None,
                    )

        dispatch(reply)

    def call_finish(self, result):
        if self.fail:
            raise GLib.Error("fake backend unavailable")
        return GLib.Variant("(o)", (self.handle,))


class PortalTests(unittest.TestCase):
    def test_sync_timeout_uses_private_context(self):
        bus = FakeBus(silent=True)
        started = time.monotonic()
        with patch.object(portal, "_session_bus", return_value=bus):
            with self.assertRaisesRegex(portal.PortalError, "вовремя"):
                portal.capture_sync(timeout_ms=25)
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(bus.subscriptions)
        self.assertEqual(len(bus.closed), 1)
        self.assertEqual(bus.call_timeout, 25)

    def test_success_and_cleanup(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as file:
            bus = FakeBus(uri=Path(file.name).as_uri())
            with patch.object(portal, "_session_bus", return_value=bus):
                self.assertEqual(portal.capture_sync(timeout_ms=100), file.name)
            self.assertFalse(bus.subscriptions)
            self.assertFalse(bus.closed)

    def test_cancel_and_backend_error_are_different(self):
        for bus, text in [
            (FakeBus(response=1), "отменён"),
            (FakeBus(response=2), "не смог"),
            (FakeBus(fail=True), "backend unavailable"),
            (FakeBus(), "пустой ответ"),
        ]:
            with self.subTest(text=text), patch.object(portal, "_session_bus", return_value=bus):
                with self.assertRaisesRegex(portal.PortalError, text):
                    portal.capture_sync(timeout_ms=100)
                self.assertFalse(bus.subscriptions)

    def test_connection_failure_does_not_start_loop(self):
        with patch.object(portal, "_session_bus", side_effect=portal.PortalError("offline")):
            with self.assertRaisesRegex(portal.PortalError, "offline"):
                portal.capture_sync(timeout_ms=20)

    def test_unsafe_uris_rejected(self):
        for uri in [
            "https://example.org/shot.png",
            "file://remote/tmp/shot.png",
            "relative.png",
            "file:///tmp/a%00b",
            "file:///tmp/a?foo=1",
            None,
        ]:
            with self.subTest(uri=uri), self.assertRaises(portal.PortalError):
                portal._uri_to_path(uri)
        self.assertEqual(portal._uri_to_path("file:///tmp/a%20b.png"), "/tmp/a b.png")

    def test_tokens_unique(self):
        self.assertEqual(len({portal._new_handle_token() for _ in range(1000)}), 1000)

    def test_send_failure_cleans_up(self):
        bus = FakeBus()
        bus.call = Mock(side_effect=GLib.Error("connection closed"))
        with patch.object(portal, "_session_bus", return_value=bus):
            with self.assertRaisesRegex(portal.PortalError, "connection closed"):
                portal.capture_sync(timeout_ms=50)
        self.assertFalse(bus.subscriptions)

    def test_callback_runs_only_once_even_for_duplicate_response(self):
        bus = FakeBus(silent=True)
        done = Mock()
        with patch.object(portal, "_session_bus", return_value=bus):
            portal.capture_async(done, timeout_ms=50)
            handler = next(iter(bus.subscriptions.values()))[1]
            reply = GLib.Variant("(ua{sv})", (1, {}))
            handler(bus, None, None, None, None, reply, None)
            handler(bus, None, None, None, None, reply, None)
        done.assert_called_once()
        self.assertFalse(bus.subscriptions)

    def test_invalid_timeout_rejected(self):
        for milliseconds in [0, -1]:
            with self.assertRaises(ValueError):
                portal.capture_sync(timeout_ms=milliseconds)


class StorageTests(unittest.TestCase):
    def test_png_collision_and_unique_names(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "shot.png"
            target.write_bytes(b"keep original")
            with self.assertRaises(FileExistsError):
                save_png(image(), target)
            first = save_png(image(), target, unique=True)
            second = save_png(image(), target, unique=True)
            self.assertNotEqual(first, second)
            self.assertEqual(target.read_bytes(), b"keep original")
            self.assertFalse(list(Path(directory).glob(".shotmark-*")))

    def test_capture_copy_preserves_source_and_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "in.png", Path(directory) / "out.png"
            source.write_bytes(b"original")
            copy_capture(source, target)
            self.assertEqual(source.read_bytes(), target.read_bytes())
            with self.assertRaises(FileExistsError):
                copy_capture(source, target)
            with self.assertRaises(FileExistsError):
                copy_capture(source, source)
            self.assertEqual(source.read_bytes(), b"original")

    def test_failed_render_leaves_no_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "bad.png"
            failing = Mock()

            def write(stream):
                stream.write(b"partial")
                raise OSError("disk full")

            failing.write_to_png.side_effect = write
            with self.assertRaises(OSError):
                save_png(failing, target)
            self.assertEqual(list(Path(directory).iterdir()), [])


class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Gtk.init()
        cls.app = Gtk.Application(
            application_id="io.github.shotmark.Tests", flags=Gio.ApplicationFlags.NON_UNIQUE
        )
        cls.app.register(None)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "source.png"
        image().write_to_png(str(path))
        self.finished = Mock()
        self.window = editor.EditorWindow(self.app, str(path), on_finish=self.finished)

    def tearDown(self):
        self.window.destroy()
        self.directory.cleanup()

    def test_horizontal_and_vertical_arrows(self):
        for end in [(80, 10), (10, 60), (80, 60)]:
            ann = editor.Annotation(editor.Tool.ARROW, [(10, 10), end])
            self.assertTrue(self.window._is_meaningful(ann))
        self.assertFalse(
            self.window._is_meaningful(editor.Annotation(editor.Tool.ARROW, [(10, 10), (10, 10)]))
        )

    def test_drag_preview_and_final_crop_match(self):
        w = self.window
        w.drag_start, w.drag_current = (80.5, 60.4), (10.2, 10.9)
        self.assertEqual(w._selection_bounds(), (10, 10, 81, 61))
        w._apply_selection(*w.drag_start, *w.drag_current)
        w.drag_start = w.drag_current = None
        self.assertEqual(w._selection_bounds(), (10, 10, 81, 61))
        result = w.render_result()
        self.assertEqual((result.get_width(), result.get_height()), (71, 51))

    def test_render_preserves_cairo_fill_rule(self):
        ctx = cairo.Context(image())
        ctx.set_fill_rule(cairo.FILL_RULE_WINDING)
        self.window.selection = (10, 10, 80, 50)
        self.window._draw_dim(ctx)
        self.assertEqual(ctx.get_fill_rule(), cairo.FILL_RULE_WINDING)

    def test_text_shortcuts_do_not_copy_or_exit(self):
        self.window._start_text_input(15, 15)
        for key in [Gdk.KEY_c, Gdk.KEY_z, Gdk.KEY_s, Gdk.KEY_Return]:
            self.assertFalse(
                self.window._on_key_pressed(None, key, 0, Gdk.ModifierType.CONTROL_MASK)
            )
        self.finished.assert_not_called()
        self.assertIsNotNone(self.window.text_entry)

    def test_copy_keeps_owner_alive_and_texture_readable(self):
        self.window._copy_and_close()
        self.finished.assert_not_called()
        clipboard = Gdk.Display.get_default().get_clipboard()
        loop = GLib.MainLoop()
        results = []

        def received(source, result, _data):
            results.append(source.read_texture_finish(result))
            loop.quit()

        clipboard.read_texture_async(None, received, None)
        timeout = GLib.timeout_add(1000, lambda: (loop.quit(), False)[1])
        loop.run()
        GLib.source_remove(timeout)
        self.assertEqual(results[0].get_width(), 120)

    def test_clipboard_readable_from_another_process(self):
        self.window._copy_and_close()
        code = """
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk
Gtk.init()
loop = GLib.MainLoop()
def received(clipboard, result, data):
    texture = clipboard.read_texture_finish(result)
    print(texture.get_width(), texture.get_height(), flush=True)
    loop.quit()
Gdk.Display.get_default().get_clipboard().read_texture_async(None, received, None)
GLib.timeout_add(2000, lambda: (loop.quit(), False)[1])
loop.run()
"""
        process = subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        loop = GLib.MainLoop()
        deadline = time.monotonic() + 4

        def poll():
            if process.poll() is not None or time.monotonic() >= deadline:
                loop.quit()
                return False
            return True

        GLib.timeout_add(10, poll)
        try:
            loop.run()
            self.assertIsNotNone(process.poll(), "clipboard reader hung")
            stdout, stderr = process.communicate(timeout=1)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "120 80", stderr)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_drag_handlers_commit_horizontal_arrow(self):
        w = self.window
        w.tool = editor.Tool.ARROW
        gesture = Mock()
        gesture.get_start_point.return_value = (True, 10, 20)
        with patch.object(w, "to_image", side_effect=lambda x, y: (x, y)):
            w._on_drag_begin(gesture, 10, 20)
            w._on_drag_update(gesture, 80, 0)
            w._on_drag_end(gesture, 80, 0)
        self.assertEqual(w.annotations[0].points, [(10, 20), (90, 20)])
        self.assertIsNone(w.draft)
        self.assertIsNone(w.drag_start)

    def test_annotations_outside_crop_cannot_start(self):
        w = self.window
        w.selection = (20, 20, 60, 40)
        w.tool = editor.Tool.RECT
        with patch.object(w, "to_image", side_effect=lambda x, y: (x, y)):
            w._on_drag_begin(Mock(), 5, 5)
        self.assertIsNone(w.draft)
        self.assertIsNone(w.drag_start)

    def test_crop_export_has_source_pixels_without_dim_or_toolbar(self):
        self.window.selection = (20, 20, 60, 40)
        out = self.window.render_result()
        out.flush()
        self.assertEqual(bytes(out.get_data()), b"\xff" * (out.get_stride() * 40))

    def test_save_failure_keeps_work(self):
        self.window.annotations.append(editor.Annotation(editor.Tool.TEXT, [(10, 20)], text="test"))
        with patch.object(editor, "save_png", side_effect=OSError("no space")):
            self.window._save_and_close()
        self.assertEqual(len(self.window.annotations), 1)
        self.assertIn("no space", self.window.status_label.get_text())
        self.finished.assert_not_called()

    def test_repeated_save_does_not_overwrite(self):
        path = str(Path(self.directory.name) / "export.png")
        with patch.object(self.window, "default_output_path", return_value=path):
            self.window._save_and_close()
            self.window._save_and_close()
        self.assertTrue(Path(path).exists())
        self.assertTrue(Path(self.directory.name, "export-1.png").exists())
        self.finished.assert_not_called()

    def test_native_window_maps_on_virtual_display(self):
        self.window.present()
        loop = GLib.MainLoop()
        GLib.timeout_add(100, lambda: (loop.quit(), False)[1])
        loop.run()
        self.assertTrue(self.window.get_mapped())
        self.assertGreater(self.window.canvas.get_width(), 0)
        self.assertTrue(self.window.toolbar.get_mapped())


class ApplicationTests(unittest.TestCase):
    def test_capture_application_waits_for_delayed_callback(self):
        options = shotmark.build_parser().parse_args([])
        app = shotmark.ShotmarkApp(options)
        completed = []

        def capture(callback, **kwargs):
            def respond():
                completed.append(True)
                callback(None, "test cancellation")
                return False

            GLib.timeout_add(40, respond)

        with patch.object(portal, "capture_async", side_effect=capture):
            app.run([])
        self.assertEqual(completed, [True])
        self.assertEqual(app.exit_code, 1)
        self.assertFalse(app._capture_pending)

    def test_existing_capture_output_refused_before_portal_call(self):
        with tempfile.NamedTemporaryFile() as file, patch.object(portal, "capture_sync") as capture:
            self.assertEqual(shotmark.main(["--capture-only", file.name]), 1)
            capture.assert_not_called()

    def test_cli_selftest_uses_synthetic_image(self):
        with tempfile.TemporaryDirectory() as directory:
            target = str(Path(directory) / "selftest.png")
            result = subprocess.run(
                [sys.executable, "shotmark.py", "--selftest", target],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            surface = cairo.ImageSurface.create_from_png(target)
            self.assertEqual((surface.get_width(), surface.get_height()), (960, 640))
            again = subprocess.run(
                [sys.executable, "shotmark.py", "--selftest", target],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(again.returncode, 0)


if __name__ == "__main__":
    unittest.main()
