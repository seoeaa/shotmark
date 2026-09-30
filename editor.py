"""Полноэкранный редактор аннотаций.

Устройство простое: на весь экран открывается окно, в котором нарисован
снимок, а поверх — слой аннотаций на Cairo. Пока область не выделена,
экран притемнён; после выделения инструменты рисуют только внутри рамки.

Все координаты аннотаций хранятся в пикселях исходного изображения, а не
в координатах окна: так результат не зависит от масштаба экрана (HiDPI)
и от того, как картинка вписалась в окно.
"""

from __future__ import annotations

import io
import math
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import cairo

from cairo_bootstrap import import_gi
from storage import save_png

gi = import_gi()

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

MOSAIC_BLOCK = 14
MIN_SELECTION = 8


class Tool(str, Enum):
    SELECT = "select"
    ARROW = "arrow"
    RECT = "rect"
    ELLIPSE = "ellipse"
    PEN = "pen"
    MARKER = "marker"
    TEXT = "text"
    MOSAIC = "mosaic"
    NUMBER = "number"


TOOL_LABELS: list[tuple[Tool, str, str]] = [
    (Tool.SELECT, "Область", "Потяните мышью для выбора области; Enter — копировать"),
    (Tool.ARROW, "Стрелка", "Стрелка"),
    (Tool.RECT, "Рамка", "Прямоугольник"),
    (Tool.ELLIPSE, "Эллипс", "Эллипс"),
    (Tool.PEN, "Перо", "Свободная линия"),
    (Tool.MARKER, "Маркер", "Полупрозрачный маркер"),
    (Tool.TEXT, "Текст", "Текст: клик по месту, ввод, Enter"),
    (Tool.MOSAIC, "Мозаика", "Скрыть область мозаикой"),
    (Tool.NUMBER, "Цифра", "Пронумерованная метка"),
]

PALETTE: list[tuple[str, tuple[float, float, float]]] = [
    ("Красный", (0.94, 0.27, 0.27)),
    ("Жёлтый", (0.98, 0.82, 0.22)),
    ("Зелёный", (0.30, 0.80, 0.40)),
    ("Голубой", (0.30, 0.65, 0.98)),
    ("Белый", (1.00, 1.00, 1.00)),
    ("Чёрный", (0.08, 0.08, 0.10)),
]


@dataclass
class Annotation:
    tool: Tool
    points: list[tuple[float, float]] = field(default_factory=list)
    color: tuple[float, float, float] = (0.94, 0.27, 0.27)
    width: float = 4.0
    text: str = ""
    #: готовая миниатюра для мозаики (создаётся один раз при фиксации)
    mosaic: Optional[cairo.ImageSurface] = None
    font_size: float = 32.0


# --------------------------------------------------------------------------
# Отрисовка аннотаций
# --------------------------------------------------------------------------


def _draw_arrow(ctx: cairo.Context, x0, y0, x1, y1, width: float) -> None:
    ctx.set_line_width(width)
    ctx.move_to(x0, y0)
    ctx.line_to(x1, y1)
    ctx.stroke()

    angle = math.atan2(y1 - y0, x1 - x0)
    head = max(width * 3.5, 12.0)
    for delta in (math.radians(28), -math.radians(28)):
        ctx.move_to(x1, y1)
        ctx.line_to(
            x1 - head * math.cos(angle + delta),
            y1 - head * math.sin(angle + delta),
        )
    ctx.stroke()


def _draw_text(ctx: cairo.Context, x, y, text: str, color, size: float) -> None:
    if not text:
        return
    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(size)
    # белая подложка-обводка, чтобы текст читался на любом фоне
    ctx.move_to(x, y)
    ctx.set_line_width(max(3.0, size / 7.0))
    ctx.set_source_rgba(1, 1, 1, 0.92)
    ctx.text_path(text)
    ctx.stroke_preserve()
    ctx.set_source_rgb(*color)
    ctx.fill()


def _draw_mosaic(ctx: cairo.Context, ann: Annotation) -> None:
    if ann.mosaic is None or len(ann.points) < 2:
        return
    (x0, y0), (x1, y1) = ann.points[0], ann.points[1]
    x, y = min(x0, x1), min(y0, y1)
    w, h = abs(x1 - x0), abs(y1 - y0)
    if w < 2 or h < 2:
        return
    mw, mh = ann.mosaic.get_width(), ann.mosaic.get_height()
    ctx.save()
    ctx.rectangle(x, y, w, h)
    ctx.clip()
    ctx.translate(x, y)
    ctx.scale(w / mw, h / mh)
    ctx.set_source_surface(ann.mosaic, 0, 0)
    ctx.get_source().set_filter(cairo.FILTER_NEAREST)
    ctx.paint()
    ctx.restore()


def _draw_number(ctx: cairo.Context, x, y, value: int, color, size: float) -> None:
    radius = size * 0.62
    ctx.set_line_width(max(2.5, size / 8.0))
    ctx.set_source_rgb(*color)
    ctx.arc(x + radius, y + radius, radius, 0, 2 * math.pi)
    ctx.fill_preserve()
    ctx.set_source_rgba(1, 1, 1, 0.95)
    ctx.stroke()

    label = str(value)
    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(size)
    extents = ctx.text_extents(label)
    ctx.move_to(
        x + radius - extents.width / 2 - extents.x_bearing,
        y + radius - extents.height / 2 - extents.y_bearing,
    )
    ctx.set_source_rgb(1, 1, 1)
    ctx.show_text(label)


def render_annotations(ctx: cairo.Context, annotations: list[Annotation]) -> None:
    for ann in annotations:
        ctx.save()
        ctx.new_path()
        ctx.set_fill_rule(cairo.FILL_RULE_WINDING)
        if ann.tool in (Tool.PEN, Tool.MARKER) and len(ann.points) > 1:
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            ctx.set_line_join(cairo.LINE_JOIN_ROUND)
            if ann.tool is Tool.MARKER:
                ctx.set_source_rgba(*ann.color, 0.42)
                ctx.set_line_width(ann.width * 3.2)
            else:
                ctx.set_source_rgb(*ann.color)
                ctx.set_line_width(ann.width)
            ctx.move_to(*ann.points[0])
            for point in ann.points[1:]:
                ctx.line_to(*point)
            ctx.stroke()

        elif ann.tool is Tool.ARROW and len(ann.points) > 1:
            ctx.set_source_rgb(*ann.color)
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            _draw_arrow(ctx, *ann.points[0], *ann.points[1], ann.width)

        elif ann.tool in (Tool.RECT, Tool.ELLIPSE) and len(ann.points) > 1:
            (x0, y0), (x1, y1) = ann.points[0], ann.points[1]
            x, y = min(x0, x1), min(y0, y1)
            w, h = abs(x1 - x0), abs(y1 - y0)
            ctx.set_source_rgb(*ann.color)
            ctx.set_line_width(ann.width)
            if ann.tool is Tool.RECT:
                ctx.rectangle(x, y, w, h)
            else:
                ctx.save()
                ctx.translate(x + w / 2, y + h / 2)
                ctx.scale(w / 2 or 1, h / 2 or 1)
                ctx.arc(0, 0, 1, 0, 2 * math.pi)
                ctx.restore()
            ctx.stroke()

        elif ann.tool is Tool.TEXT and ann.points:
            _draw_text(ctx, *ann.points[0], ann.text, ann.color, ann.font_size)

        elif ann.tool is Tool.MOSAIC:
            _draw_mosaic(ctx, ann)

        elif ann.tool is Tool.NUMBER and ann.points:
            _draw_number(ctx, *ann.points[0], int(ann.text), ann.color, ann.font_size)
        ctx.restore()


# --------------------------------------------------------------------------
# Окно редактора
# --------------------------------------------------------------------------


def surface_to_texture(surface: cairo.ImageSurface) -> Gdk.Texture:
    """cairo-поверхность -> текстура для буфера обмена.

    Через PNG в памяти: pycairo и PyGObject не разделяют тип Surface, а
    deprecated Gdk.pixbuf_get_from_surface на нём падает.
    """
    buffer = io.BytesIO()
    surface.write_to_png(buffer)
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(buffer.getvalue()))


class EditorWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, image_path: str, on_finish=None):
        super().__init__(application=app)
        self.image_path = image_path
        self.on_finish = on_finish

        self.surface = cairo.ImageSurface.create_from_png(image_path)
        self.image_w = self.surface.get_width()
        self.image_h = self.surface.get_height()

        self.annotations: list[Annotation] = []
        self.selection: Optional[tuple[float, float, float, float]] = None
        self.tool = Tool.SELECT
        self.color: tuple[float, float, float] = PALETTE[0][1]
        self.line_width = 4.0
        self.number_counter = 0

        self.drag_start: Optional[tuple[float, float]] = None
        self.drag_current: Optional[tuple[float, float]] = None
        self.draft: Optional[Annotation] = None
        self.text_entry: Optional[Gtk.Entry] = None
        self.text_anchor: tuple[float, float] = (0.0, 0.0)

        self._build_ui()
        self._build_controllers()

    # ---------------------------------------------------------------- UI

    def _build_ui(self) -> None:
        self.set_title("Shotmark")
        self.set_decorated(False)

        self.canvas = Gtk.DrawingArea()
        self.canvas.set_draw_func(self._on_draw)
        self.canvas.set_hexpand(True)
        self.canvas.set_vexpand(True)
        self.canvas.set_cursor(Gdk.Cursor.new_from_name("crosshair"))

        self.toolbar = self._build_toolbar()

        overlay = Gtk.Overlay()
        overlay.set_child(self.canvas)
        overlay.add_overlay(self.toolbar)
        self.toolbar.set_halign(Gtk.Align.CENTER)
        self.toolbar.set_valign(Gtk.Align.END)
        self.toolbar.set_margin_bottom(28)
        self.set_child(overlay)
        self.overlay = overlay

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key)

        # Размер до показа: если fullscreen почему-то не сработает,
        # окно всё равно останется пригодным для работы.
        self.set_default_size(max(960, min(self.image_w, 1600)), max(600, min(self.image_h, 900)))

    def enter_fullscreen(self) -> None:
        """Растянуть на весь экран. Вызывать после present()."""
        self.fullscreen()

    def _build_toolbar(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("toolbar")
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(10)
        box.set_margin_bottom(10)

        # --- инструменты ---
        tools_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.tool_buttons: dict[Tool, Gtk.ToggleButton] = {}
        first: Optional[Gtk.ToggleButton] = None
        for tool, label, hint in TOOL_LABELS:
            button = Gtk.ToggleButton(label=label)
            button.set_tooltip_text(hint)
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.connect("toggled", self._on_tool_toggled, tool)
            self.tool_buttons[tool] = button
            tools_row.append(button)
        self.tool_buttons[Tool.SELECT].set_active(True)
        box.append(tools_row)

        # --- цвета и толщина ---
        options_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        options_row.set_halign(Gtk.Align.CENTER)

        self.color_buttons: dict[str, Gtk.ToggleButton] = {}
        first_color: Optional[Gtk.ToggleButton] = None
        for name, rgb in PALETTE:
            swatch = Gtk.ToggleButton()
            swatch.set_tooltip_text(name)
            swatch.set_size_request(26, 26)
            # Образец — отдельный виджет. Глобальных CSS-правил нет;
            # checked/focus/hover продолжает рисовать текущая тема GTK.
            chip = Gtk.DrawingArea()
            chip.set_content_width(18)
            chip.set_content_height(18)

            def draw_color(_area, ctx, _width, _height, color=rgb):
                ctx.set_source_rgb(*color)
                ctx.paint()

            chip.set_draw_func(draw_color)
            swatch.set_child(chip)
            swatch.update_property([Gtk.AccessibleProperty.LABEL], [name])
            if first_color is None:
                first_color = swatch
            else:
                swatch.set_group(first_color)
            swatch.connect("toggled", self._on_color_toggled, rgb)
            self.color_buttons[name] = swatch
            options_row.append(swatch)
        self.color_buttons[PALETTE[0][0]].set_active(True)

        options_row.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        width_label = Gtk.Label(label="Толщина")
        options_row.append(width_label)
        self.width_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 20, 1)
        self.width_scale.set_value(self.line_width)
        self.width_scale.set_size_request(150, -1)
        self.width_scale.set_draw_value(False)
        self.width_scale.connect("value-changed", self._on_width_changed)
        options_row.append(self.width_scale)

        options_row.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        undo = Gtk.Button(label="Отменить")
        undo.connect("clicked", lambda *_: self._undo())
        options_row.append(undo)

        clear = Gtk.Button(label="Очистить")
        clear.connect("clicked", lambda *_: self._clear_annotations())
        options_row.append(clear)

        copy = Gtk.Button(label="Копировать")
        copy.add_css_class("suggested-action")
        copy.connect("clicked", lambda *_: self._copy_and_close())
        options_row.append(copy)

        save = Gtk.Button(label="Сохранить")
        save.connect("clicked", lambda *_: self._save_and_close())
        options_row.append(save)

        close = Gtk.Button(label="Закрыть")
        close.connect("clicked", lambda *_: self._close())
        options_row.append(close)

        box.append(options_row)
        self.status_label = Gtk.Label(label="Выделите область или рисуйте на всём снимке")
        self.status_label.set_wrap(True)
        box.append(self.status_label)
        return box

    def _build_controllers(self) -> None:
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.canvas.add_controller(drag)

        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self.canvas.add_controller(click)

    # ------------------------------------------------- координаты/масштаб

    def _transform(self) -> tuple[float, float, float]:
        """(scale, offset_x, offset_y): пиксели картинки -> логика окна."""
        width = max(1, self.canvas.get_width())
        height = max(1, self.canvas.get_height())
        scale = max(self.image_w / width, self.image_h / height)
        drawn_w, drawn_h = self.image_w / scale, self.image_h / scale
        return scale, (width - drawn_w) / 2, (height - drawn_h) / 2

    def to_image(self, x: float, y: float) -> tuple[float, float]:
        scale, ox, oy = self._transform()
        return (x - ox) * scale, (y - oy) * scale

    def _clamp(self, x: float, y: float) -> tuple[float, float]:
        return (
            min(max(x, 0.0), float(self.image_w)),
            min(max(y, 0.0), float(self.image_h)),
        )

    # ------------------------------------------------------------- отрисовка

    def _on_draw(self, _area, ctx: cairo.Context, width: int, height: int) -> None:
        ctx.set_source_rgb(0.07, 0.07, 0.08)
        ctx.paint()

        scale, ox, oy = self._transform()
        ctx.save()
        ctx.translate(ox, oy)
        ctx.scale(1 / scale, 1 / scale)

        ctx.set_source_surface(self.surface, 0, 0)
        ctx.get_source().set_filter(cairo.FILTER_GOOD)
        ctx.paint()

        self._draw_dim(ctx)
        ctx.save()
        x0, y0, x1, y1 = self._selection_bounds()
        ctx.rectangle(x0, y0, x1 - x0, y1 - y0)
        ctx.clip()
        render_annotations(ctx, self.annotations)
        if self.draft is not None:
            render_annotations(ctx, [self.draft])
        ctx.restore()
        self._draw_selection_marks(ctx)

        ctx.restore()

    def _draw_dim(self, ctx: cairo.Context) -> None:
        """Притемнить всё, кроме выделенной области."""
        x0, y0, x1, y1 = self._selection_bounds()
        ctx.save()
        ctx.new_path()
        ctx.set_source_rgba(0, 0, 0, 0.55)
        ctx.rectangle(0, 0, self.image_w, self.image_h)
        if x1 > x0 and y1 > y0:
            ctx.rectangle(x0, y0, x1 - x0, y1 - y0)
            ctx.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        ctx.fill()
        ctx.restore()

    def _draw_selection_marks(self, ctx: cairo.Context) -> None:
        x0, y0, x1, y1 = self._selection_bounds()
        if x1 <= x0 or y1 <= y0:
            return
        ctx.set_source_rgba(1, 1, 1, 0.9)
        ctx.set_line_width(1.5)
        ctx.rectangle(x0, y0, x1 - x0, y1 - y0)
        ctx.stroke()
        # уголки
        ctx.set_line_width(4)
        size = min(28.0, (x1 - x0) / 4, (y1 - y0) / 4)
        for cx, cy, dx, dy in (
            (x0, y0, 1, 1),
            (x1, y0, -1, 1),
            (x0, y1, 1, -1),
            (x1, y1, -1, -1),
        ):
            ctx.move_to(cx + dx * size, cy)
            ctx.line_to(cx, cy)
            ctx.line_to(cx, cy + dy * size)
        ctx.stroke()

    def _selection_bounds(self) -> tuple[float, float, float, float]:
        """Рамка выделения в пикселях картинки; без выделения — весь кадр."""
        if self.tool is Tool.SELECT and self.drag_start and self.drag_current:
            x0, y0 = self.drag_start
            x1, y1 = self.drag_current
            return (
                math.floor(min(x0, x1)),
                math.floor(min(y0, y1)),
                math.ceil(max(x0, x1)),
                math.ceil(max(y0, y1)),
            )
        if self.selection is None:
            return 0.0, 0.0, float(self.image_w), float(self.image_h)
        x, y, w, h = self.selection
        return x, y, x + w, y + h

    def _inside_selection(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self._selection_bounds()
        return x0 <= x <= x1 and y0 <= y <= y1

    # ---------------------------------------------------------- инструменты

    def _on_tool_toggled(self, button: Gtk.ToggleButton, tool: Tool) -> None:
        if button.get_active():
            self._commit_text_input()
            self.tool = tool
            self.canvas.queue_draw()

    def _on_color_toggled(self, button: Gtk.ToggleButton, rgb) -> None:
        if button.get_active():
            self.color = rgb
            self.canvas.queue_draw()

    def _on_width_changed(self, scale: Gtk.Scale) -> None:
        self.line_width = float(scale.get_value())

    def _font_size(self) -> float:
        return max(16.0, self.image_w * 0.018)

    # ------------------------------------------------------ обработка мыши

    def _on_drag_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        if self.tool in (Tool.TEXT, Tool.NUMBER):
            return
        ix, iy = self._clamp(*self.to_image(x, y))
        self.drag_start = (ix, iy)
        self.drag_current = (ix, iy)
        if self.tool is not Tool.SELECT and not self._inside_selection(ix, iy):
            self.drag_start = self.drag_current = None
            return
        if self.tool not in (Tool.SELECT,):
            self.draft = Annotation(
                tool=self.tool,
                points=[(ix, iy), (ix, iy)],
                color=self.color,
                width=self.line_width * (1.6 if self.tool is Tool.MARKER else 1.0),
                font_size=self._font_size(),
            )
        self.canvas.queue_draw()

    def _on_drag_update(self, gesture: Gtk.GestureDrag, ox: float, oy: float) -> None:
        if self.drag_start is None:
            return
        start_x, start_y = self.drag_start
        widget_x, widget_y = self._offset_to_widget(gesture, ox, oy)
        ix, iy = self._clamp(*self.to_image(widget_x, widget_y))
        self.drag_current = (ix, iy)

        if self.draft is not None:
            if self.draft.tool in (Tool.PEN, Tool.MARKER):
                self.draft.points.append((ix, iy))
            else:
                self.draft.points = [(start_x, start_y), (ix, iy)]
        self.canvas.queue_draw()

    def _offset_to_widget(self, gesture: Gtk.GestureDrag, ox: float, oy: float):
        ok, sx, sy = gesture.get_start_point()
        if not ok:
            return 0.0, 0.0
        return sx + ox, sy + oy

    def _on_drag_end(self, gesture: Gtk.GestureDrag, ox: float, oy: float) -> None:
        if self.drag_start is None:
            return
        widget_x, widget_y = self._offset_to_widget(gesture, ox, oy)
        ix, iy = self._clamp(*self.to_image(widget_x, widget_y))

        if self.tool is Tool.SELECT:
            self._apply_selection(*self.drag_start, ix, iy)
        elif self.draft is not None:
            if self.draft.tool in (Tool.PEN, Tool.MARKER):
                self.draft.points.append((ix, iy))
            else:
                self.draft.points = [self.drag_start, (ix, iy)]
            if self._is_meaningful(self.draft):
                if self.draft.tool is Tool.MOSAIC:
                    self.draft.mosaic = self._make_mosaic(self.draft)
                self.annotations.append(self.draft)

        self.draft = None
        self.drag_start = None
        self.drag_current = None
        self.canvas.queue_draw()

    def _is_meaningful(self, ann: Annotation) -> bool:
        if len(ann.points) < 2:
            return False
        if ann.tool in (Tool.PEN, Tool.MARKER):
            return any(math.dist(ann.points[0], p) > 2 for p in ann.points[1:])
        (x0, y0), (x1, y1) = ann.points[0], ann.points[1]
        if ann.tool is Tool.ARROW:
            return math.hypot(x1 - x0, y1 - y0) > 2
        return abs(x1 - x0) > 2 and abs(y1 - y0) > 2

    def _apply_selection(self, x0, y0, x1, y1) -> None:
        x, y = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        if w < MIN_SELECTION or h < MIN_SELECTION:
            self.selection = None
        else:
            left, top = math.floor(x), math.floor(y)
            self.selection = (left, top, math.ceil(x + w) - left, math.ceil(y + h) - top)

    def _on_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        if not self._inside_selection(*self.to_image(x, y)):
            return
        if self.tool is Tool.NUMBER:
            ix, iy = self._clamp(*self.to_image(x, y))
            self.number_counter += 1
            self.annotations.append(
                Annotation(
                    tool=Tool.NUMBER,
                    points=[(ix, iy)],
                    color=self.color,
                    text=str(self.number_counter),
                    font_size=self._font_size() * 1.1,
                )
            )
            self.canvas.queue_draw()
        elif self.tool is Tool.TEXT:
            self._start_text_input(x, y)

    # ------------------------------------------------------------ мозаика

    def _make_mosaic(self, ann: Annotation) -> cairo.ImageSurface:
        (x0, y0), (x1, y1) = ann.points[0], ann.points[1]
        x, y = min(x0, x1), min(y0, y1)
        w, h = max(abs(x1 - x0), 1.0), max(abs(y1 - y0), 1.0)
        mw = max(1, int(w / MOSAIC_BLOCK))
        mh = max(1, int(h / MOSAIC_BLOCK))

        mini = cairo.ImageSurface(cairo.FORMAT_ARGB32, mw, mh)
        ctx = cairo.Context(mini)
        ctx.scale(mw / w, mh / h)
        ctx.translate(-x, -y)
        ctx.set_source_surface(self.surface, 0, 0)
        ctx.get_source().set_filter(cairo.FILTER_GOOD)
        ctx.paint()
        return mini

    # -------------------------------------------------------------- текст

    def _start_text_input(self, x: float, y: float) -> None:
        self._commit_text_input()
        entry = Gtk.Entry()
        entry.set_placeholder_text("Текст и Enter")
        entry.set_size_request(260, -1)
        entry.set_halign(Gtk.Align.START)
        entry.set_valign(Gtk.Align.START)
        entry.set_margin_start(int(max(0, x)))
        entry.set_margin_top(int(max(0, y)))
        entry.connect("activate", lambda *_: self._commit_text_input())
        self.overlay.add_overlay(entry)
        self.text_entry = entry
        self.text_anchor = self._clamp(*self.to_image(x, y))
        entry.grab_focus()

    def _commit_text_input(self) -> None:
        entry = self.text_entry
        if entry is None:
            return
        self.text_entry = None
        text = entry.get_text().strip()
        self.overlay.remove_overlay(entry)
        if text:
            self.annotations.append(
                Annotation(
                    tool=Tool.TEXT,
                    points=[self.text_anchor],
                    color=self.color,
                    text=text,
                    font_size=self._font_size(),
                )
            )
        self.canvas.queue_draw()

    # ---------------------------------------------------------- клавиатура

    def _on_key_pressed(self, _c, keyval: int, _code: int, state: Gdk.ModifierType) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if keyval == Gdk.KEY_Escape:
            if self.text_entry is not None:
                entry = self.text_entry
                self.text_entry = None
                self.overlay.remove_overlay(entry)
                return True
            self._close()
            return True
        if self.text_entry is not None:
            # Пока вводится текст, Ctrl+C/Z и Enter принадлежат Gtk.Entry.
            return False
        if ctrl and keyval in (Gdk.KEY_c, Gdk.KEY_C):
            self._copy_and_close()
            return True
        if ctrl and keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self._save_and_close()
            return True
        if ctrl and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            self._undo()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.text_entry is None:
            self._copy_and_close()
            return True
        return False

    # ------------------------------------------------------------ действия

    def _undo(self) -> None:
        if self.annotations:
            removed = self.annotations.pop()
            if removed.tool is Tool.NUMBER:
                self.number_counter = max(0, self.number_counter - 1)
            self.canvas.queue_draw()

    def _clear_annotations(self) -> None:
        self.annotations.clear()
        self.number_counter = 0
        self.canvas.queue_draw()

    def default_output_path(self) -> str:
        pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        base = pictures or os.path.expanduser("~")
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        return os.path.join(base, f"Screenshot_{stamp}.png")

    def render_result(self) -> cairo.ImageSurface:
        x0, y0, x1, y1 = self._selection_bounds()
        width = max(1, int(round(x1 - x0)))
        height = max(1, int(round(y1 - y0)))
        out = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        ctx = cairo.Context(out)
        ctx.translate(-x0, -y0)
        ctx.set_source_surface(self.surface, 0, 0)
        ctx.paint()
        render_annotations(ctx, self.annotations)
        return out

    def _copy_and_close(self) -> None:
        # Не закрываем владельца Wayland clipboard до вставки в другое приложение.
        # Имя метода сохранено для совместимости с кнопками прототипа.
        self._commit_text_input()
        try:
            buffer = io.BytesIO()
            self.render_result().write_to_png(buffer)
            # Явный MIME вместо backend-зависимого набора форматов Gdk.Texture.
            self.clipboard_content = Gdk.ContentProvider.new_for_bytes(
                "image/png", GLib.Bytes.new(buffer.getvalue())
            )
            if not Gdk.Display.get_default().get_clipboard().set_content(self.clipboard_content):
                self.status_label.set_text("Система не приняла изображение в буфер")
                return
        except (GLib.Error, cairo.Error, OSError) as exc:
            self.status_label.set_text(f"Не удалось скопировать: {exc}")
            return
        self.status_label.set_text(
            "Скопировано. Alt+Tab → вставить. Не закрывайте редактор до вставки."
        )

    def _save_and_close(self) -> None:
        self._commit_text_input()
        try:
            path = save_png(self.render_result(), self.default_output_path(), unique=True)
        except (OSError, cairo.Error) as exc:
            self.status_label.set_text(f"Не удалось сохранить: {exc}")
            return
        self.status_label.set_text(f"Сохранено: {path}")

    def _close(self) -> None:
        self._finish(None)

    def _finish(self, message: Optional[str]) -> None:
        callback = self.on_finish
        self.on_finish = None
        if callback is not None:
            callback(message)
        self.close()
