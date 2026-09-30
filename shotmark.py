#!/usr/bin/env python3
"""Shotmark — аннотатор скриншотов для Wayland.

Примеры:
    shotmark                       # снять экран и открыть редактор
    shotmark --interactive         # доверить выбор области самому GNOME
    shotmark --file shot.png       # аннотировать готовый файл
    shotmark --capture-only out.png  # только снять экран, без редактора
    shotmark --selftest out.png    # проверить рендер движка без GUI
"""

from __future__ import annotations

import argparse
import os
import tempfile
import cairo
from storage import copy_capture, save_png
import sys

from cairo_bootstrap import import_gi

gi = import_gi()

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, Gtk  # noqa: E402

import portal  # noqa: E402
from editor import Annotation, EditorWindow, Tool, surface_to_texture  # noqa: E402

from app_info import APP_ID, VERSION, ICON_NAME, ICON_ROOT  # noqa: E402


class ShotmarkApp(Gtk.Application):
    def __init__(self, options: argparse.Namespace):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.options = options
        self.window: EditorWindow | None = None
        self.exit_code = 0
        self._capture_pending = False

    # Gtk.Application
    def do_activate(self) -> None:
        # Исключение внутри обработчика GTK не завершает приложение — цикл
        # продолжает крутиться без окна. Поэтому падаем громко и сразу выходим.
        try:
            if self.options.selftest:
                self._run_selftest()
                return
            if self.options.file:
                self._open_editor(os.path.abspath(self.options.file))
                return
            # Окна ещё нет: без hold GApplication выйдет раньше ответа портала.
            self.hold()
            self._capture_pending = True
            portal.capture_async(self._on_captured, interactive=self.options.interactive)
        except Exception as exc:  # noqa: BLE001 - хотим показать пользователю
            import traceback

            traceback.print_exc()
            self._fail(f"внутренняя ошибка: {exc}")

    # --- сценарии
    def _release_capture(self):
        if self._capture_pending:
            self._capture_pending = False
            self.release()

    def _on_captured(self, path: str | None, error: str | None) -> None:
        try:
            if error:
                self._fail(error)
            elif path:
                self._open_editor(path)
            else:
                self._fail("портал не вернул изображение")
        except Exception as exc:
            self._fail(f"не удалось открыть снимок: {exc}")
        finally:
            self._release_capture()

    def _open_editor(self, path: str) -> None:
        if not os.path.exists(path):
            self._fail(f"файл не найден: {path}")
            return
        self.window = EditorWindow(self, path, on_finish=self._on_finish)
        Gtk.IconTheme.get_for_display(self.window.get_display()).add_search_path(
            str(ICON_ROOT.parent)
        )
        self.window.set_icon_name(ICON_NAME)
        self.window.present()
        # Запрашиваем fullscreen; реальное состояние подтверждает оконный менеджер.
        self.window.enter_fullscreen()

    def _on_finish(self, message: str | None) -> None:
        if message:
            print(message)
        self.quit()

    def _fail(self, message: str) -> None:
        print(f"shotmark: ошибка: {message}", file=sys.stderr)
        self.exit_code = 1
        self._release_capture()
        self.quit()

    # --- проверка движка рендера без показа окна
    def _run_selftest(self) -> None:
        # Самопроверка не снимает рабочий стол и не требует согласия портала.
        with tempfile.TemporaryDirectory(prefix="shotmark-selftest-") as directory:
            source = self.options.file
            if not source:
                source = os.path.join(directory, "sample.png")
                sample = cairo.ImageSurface(cairo.FORMAT_ARGB32, 960, 640)
                context = cairo.Context(sample)
                context.set_source_rgb(0.18, 0.24, 0.30)
                context.paint()
                sample.write_to_png(source)
            window = EditorWindow(self, source)
        width, height = window.image_w, window.image_h
        cx, cy = width / 2, height / 2

        window.annotations.extend(
            [
                Annotation(
                    Tool.RECT,
                    [(cx - 320, cy - 180), (cx + 320, cy + 180)],
                    color=(0.94, 0.27, 0.27),
                    width=5,
                ),
                Annotation(
                    Tool.ARROW,
                    [(cx - 280, cy + 120), (cx + 260, cy - 120)],
                    color=(0.30, 0.65, 0.98),
                    width=6,
                ),
                Annotation(
                    Tool.TEXT,
                    [(cx - 320, cy - 220)],
                    color=(0.98, 0.82, 0.22),
                    text="Shotmark selftest",
                    font_size=max(20.0, width * 0.022),
                ),
                Annotation(
                    Tool.NUMBER,
                    [(cx + 180, cy + 90)],
                    color=(0.30, 0.80, 0.40),
                    text="1",
                    font_size=max(20.0, width * 0.02),
                ),
                Annotation(
                    Tool.MARKER,
                    [(cx - 300, cy + 200), (cx + 300, cy + 210)],
                    color=(0.98, 0.82, 0.22),
                    width=8,
                ),
            ]
        )
        mosaic = Annotation(
            Tool.MOSAIC, [(cx - 200, cy - 60), (cx + 200, cy + 60)], color=(0.0, 0.0, 0.0), width=0
        )
        mosaic.mosaic = window._make_mosaic(mosaic)
        window.annotations.append(mosaic)

        target = os.path.abspath(self.options.selftest)
        result = window.render_result()
        save_png(result, target)

        # тот же путь, что и при «Копировать» — конверсия в текстуру буфера
        texture = surface_to_texture(result)
        assert texture.get_width() == result.get_width(), "текстура не совпала по ширине"

        print(
            f"selftest: рендер записан в {target} "
            f"({window.image_w}x{window.image_h}, "
            f"вырез {result.get_width()}x{result.get_height()})"
        )
        self.quit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shotmark",
        description="Аннотатор скриншотов для Wayland (GNOME/KDE).",
    )
    parser.add_argument(
        "--file", "-f", metavar="PATH", help="аннотировать существующее изображение"
    )
    parser.add_argument(
        "--interactive", action="store_true", help="попросить портал показать выбор области/окна"
    )
    parser.add_argument(
        "--capture-only", metavar="PATH", help="только снять экран и сохранить в PATH"
    )
    parser.add_argument(
        "--selftest", metavar="PATH", help="отрендерить образец аннотаций в PATH (без GUI)"
    )
    parser.add_argument("--version", action="version", version=f"shotmark {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    if options.capture_only and (options.file or options.selftest):
        parser.error("--capture-only несовместим с --file/--selftest")
    if options.interactive and (options.file or options.selftest):
        parser.error("--interactive используется только для захвата")

    if options.capture_only:
        try:
            target = os.path.abspath(options.capture_only)
            if os.path.lexists(target):
                raise FileExistsError(f"файл уже существует: {target}")
            path = portal.capture_sync(interactive=options.interactive)
            copy_capture(path, target)
        except (portal.PortalError, OSError) as exc:
            print(f"shotmark: ошибка: {exc}", file=sys.stderr)
            return 1
        print(f"Сохранено: {target}")
        return 0

    app = ShotmarkApp(options)
    app.run([])
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
