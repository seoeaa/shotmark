#!/usr/bin/env python3
"""Трей Ayatana/GTK3. Редактор GTK4 запускается только отдельным процессом."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator, Gio, GLib, Gtk

from app_info import APP_ID, ICON_NAME, ICON_ROOT, VERSION

TRAY_ID = APP_ID + ".Tray"
ROOT = Path(__file__).resolve().parent


def watcher_available() -> bool:
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
            GLib.VariantType.new("(b)"),
            Gio.DBusCallFlags.NONE,
            1500,
            None,
        )
        return result.unpack()[0]
    except GLib.Error:
        return False


def editor_command(arguments=()):
    return [sys.executable, str(ROOT / "shotmark.py"), *arguments]


class TrayApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=TRAY_ID)
        self.indicator = None
        self.editor_process = None
        self.chooser = None
        self.dialogs = set()
        self.capture_items = []
        self.exit_code = 0

    def do_startup(self):
        Gtk.Application.do_startup(self)
        action = Gio.SimpleAction.new("quit", None)
        action.connect("activate", lambda *_: self.quit())
        self.add_action(action)

    def do_activate(self):
        if self.indicator is not None:
            return  # GApplication передаёт повторный запуск уже работающему трею.
        self.hold()
        if not watcher_available():
            self.exit_code = 1
            self._message(
                "Системный трей недоступен",
                "В GNOME включите расширение Ubuntu AppIndicators. "
                "Редактор можно открыть командой shotmark без --tray.",
                error=True,
                on_close=self.quit,
            )
            return
        self.indicator = AppIndicator.Indicator.new(
            "shotmark", ICON_NAME + "-symbolic", AppIndicator.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_icon_theme_path(str(ICON_ROOT / "scalable/apps"))
        self.indicator.set_title("Shotmark")
        self.menu = Gtk.Menu()
        self._item("Сделать скриншот", lambda *_: self._start_editor(), capture=True)
        self._item(
            "Системный выбор области / окна",
            lambda *_: self._start_editor(["--interactive"]),
            capture=True,
        )
        self._item("Открыть PNG…", self._choose_file, capture=True)
        self.menu.append(Gtk.SeparatorMenuItem())
        self._item("О Shotmark", self._about)
        self._item("Выход из трея", lambda *_: self.quit())
        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        self.indicator.set_secondary_activate_target(self.capture_items[0])
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        print(f"Shotmark {VERSION}: трей запущен", flush=True)

    def _item(self, label, callback, *, capture=False):
        item = Gtk.MenuItem(label=label)
        item.connect("activate", callback)
        self.menu.append(item)
        if capture:
            self.capture_items.append(item)
        return item

    def _refresh_busy(self):
        for item in self.capture_items:
            item.set_sensitive(self.editor_process is None and self.chooser is None)

    def _start_editor(self, arguments=()):
        if self.editor_process is not None:
            return
        try:
            process = Gio.Subprocess.new(
                editor_command(arguments),
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE,
            )
        except GLib.Error as exc:
            self._message("Не удалось открыть редактор", str(exc), error=True)
            return
        self.editor_process = process
        self._refresh_busy()
        process.communicate_utf8_async(None, None, self._editor_finished)

    def _editor_finished(self, process, result):
        try:
            _, output, _ = process.communicate_utf8_finish(result)
            if not process.get_successful():
                self._message(
                    "Редактор завершился с ошибкой",
                    (output or "Нет подробностей")[-2000:],
                    error=True,
                )
        except GLib.Error as exc:
            self._message("Ошибка связи с редактором", str(exc), error=True)
        finally:
            self.editor_process = None
            self._refresh_busy()

    def _choose_file(self, *_args):
        if self.chooser is not None or self.editor_process is not None:
            return
        chooser = Gtk.FileChooserNative.new(
            "Открыть PNG в Shotmark", None, Gtk.FileChooserAction.OPEN, "Открыть", "Отмена"
        )
        chooser.set_local_only(True)
        png = Gtk.FileFilter()
        png.set_name("PNG-изображения")
        png.add_mime_type("image/png")
        chooser.add_filter(png)
        chooser.connect("response", self._file_chosen)
        self.chooser = chooser
        self._refresh_busy()
        chooser.show()

    def _file_chosen(self, chooser, response):
        path = chooser.get_filename() if response == Gtk.ResponseType.ACCEPT else None
        chooser.destroy()
        self.chooser = None
        self._refresh_busy()
        if path:
            self._start_editor(["--file", path])

    def _message(self, title, text, *, error=False, on_close=None):
        dialog = Gtk.MessageDialog(
            message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.set_application(self)
        dialog.format_secondary_text(text)
        self.dialogs.add(dialog)

        def close(*_args):
            self.dialogs.discard(dialog)
            dialog.destroy()
            if on_close:
                on_close()

        dialog.connect("response", close)
        dialog.present()

    def _about(self, *_args):
        self._message(
            f"Shotmark {VERSION}",
            "Аннотатор скриншотов для Wayland.\n"
            "Ctrl+C — копировать PNG; Ctrl+S — сохранить; Esc — закрыть редактор.\n"
            "Для вставки переключитесь Alt+Tab, не закрывая редактор.\n"
            "Пока редактор открыт, повторный захват из трея недоступен.\n"
            "Выход из трея не закрывает открытый редактор.",
        )

    def do_shutdown(self):
        if self.indicator is not None:
            self.indicator.set_status(AppIndicator.IndicatorStatus.PASSIVE)
        if self.chooser is not None:
            self.chooser.destroy()
        # Не убиваем дочерний редактор: там могут быть несохранённые аннотации.
        Gtk.Application.do_shutdown(self)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Значок Shotmark в системном трее")
    parser.add_argument("--quit", action="store_true", help="закрыть работающий трей")
    parser.add_argument("--check", action="store_true", help="проверить поддержку трея")
    options = parser.parse_args(argv)
    if options.check:
        available = watcher_available()
        print(f"GTK3/Ayatana: OK; StatusNotifierWatcher: {'OK' if available else 'нет'}")
        return 0 if available else 1
    if options.quit:
        # Не регистрируем временный Gtk.Application ради команды выхода.
        # Это создавало предупреждение об отсутствии unregister при разрушении.
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync(
                TRAY_ID,
                "/" + TRAY_ID.replace(".", "/"),
                "org.gtk.Actions",
                "Activate",
                GLib.Variant("(sava{sv})", ("quit", [], {})),
                None,
                Gio.DBusCallFlags.NO_AUTO_START,
                1500,
                None,
            )
        except GLib.Error as exc:
            name = Gio.DBusError.get_remote_error(exc)
            if name not in (
                "org.freedesktop.DBus.Error.ServiceUnknown",
                "org.freedesktop.DBus.Error.NameHasNoOwner",
            ):
                print(f"Не удалось закрыть трей: {exc}", file=sys.stderr)
                return 1
        return 0
    app = TrayApp()
    status = app.run([sys.argv[0]])
    return app.exit_code or status


if __name__ == "__main__":
    raise SystemExit(main())
