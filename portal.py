"""Захват экрана через xdg-desktop-portal.

На Wayland (GNOME, KDE) приложение не имеет права читать чужие пиксели
напрямую — только через портал `org.freedesktop.portal.Screenshot`.

Портал возвращает снимок целиком (или выбранное пользователем окно при
interactive=True) и отдаёт путь к PNG-файлу в ответе на DBus-запрос.

Здесь две точки входа:
  * capture_sync()  — блокирующая, для CLI и тестов;
  * capture_async() — колбэк в главном цикле GTK, без блокировки UI.
"""

from __future__ import annotations

import os
import uuid
import urllib.parse
from typing import Callable, Optional

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
SCREENSHOT_INTERFACE = "org.freedesktop.portal.Screenshot"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"

#: Портал спрашивает пользователя (диалог «Поделиться экраном») — ждём щедро.
DEFAULT_TIMEOUT_MS = 120_000


class PortalError(RuntimeError):
    """Портал недоступен, отказал или пользователь отменил запрос."""


def _session_bus() -> Gio.DBusConnection:
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as exc:  # pragma: no cover - зависит от окружения
        raise PortalError(f"нет доступа к сессионной шине D-Bus: {exc.message}") from exc


def _sender_token(connection: Gio.DBusConnection) -> str:
    """`:1.42` -> `1_42` — так портал строит путь запроса."""
    return connection.get_unique_name().lstrip(":").replace(".", "_")


def _request_path(connection: Gio.DBusConnection, handle_token: str) -> str:
    return f"{PORTAL_OBJECT_PATH}/request/" f"{_sender_token(connection)}/{handle_token}"


def _uri_to_path(uri: str) -> str:
    if not isinstance(uri, str):
        raise PortalError("портал вернул некорректный URI")
    try:
        parsed = urllib.parse.urlparse(uri)
        path = urllib.parse.unquote(parsed.path)
    except ValueError as exc:
        raise PortalError("портал вернул некорректный URI") from exc
    if (
        parsed.scheme != "file"
        or parsed.netloc not in ("", "localhost")
        or parsed.query
        or parsed.fragment
        or not os.path.isabs(path)
        or "\x00" in path
    ):
        raise PortalError("портал вернул не локальный файловый URI")
    return path


def _build_options(interactive: bool, handle_token: str) -> dict:
    return {
        "handle_token": GLib.Variant("s", handle_token),
        # interactive=True → GNOME покажет свой выбор области/окна.
        # interactive=False → сразу весь экран (и наш собственный редактор).
        "interactive": GLib.Variant("b", interactive),
    }


def _new_handle_token() -> str:
    return f"shotmark_{uuid.uuid4().hex}"


# --------------------------------------------------------------------------
# Синхронный вариант — для CLI и автотестов (без запущенного цикла GTK)
# --------------------------------------------------------------------------


def capture_sync(
    interactive: bool = False,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> str:
    """Блокирующая обёртка над тем же запросом в отдельном GLib-контексте."""
    context = GLib.MainContext.new()
    context.push_thread_default()
    loop = GLib.MainLoop.new(context, False)
    outcome = []

    def completed(path, error):
        outcome.append((path, error))
        loop.quit()

    try:
        capture_async(completed, interactive=interactive, timeout_ms=timeout_ms)
        if not outcome:  # ошибка подключения может завершить запрос сразу
            loop.run()
    finally:
        context.pop_thread_default()
    path, error = outcome[0]
    if error:
        raise PortalError(error)
    return path


# --------------------------------------------------------------------------
# Асинхронный вариант — для GTK (UI не замирает, пока пользователь решает)
# --------------------------------------------------------------------------


def capture_async(
    callback: Callable[[Optional[str], Optional[str]], None],
    interactive: bool = False,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> None:
    """Запросить скриншот, не блокируя интерфейс.

    `callback(path, error)`: ровно один из аргументов равен None.
    Вызывается в главном цикле GLib, из него безопасно трогать виджеты.
    """
    if timeout_ms <= 0:
        raise ValueError("timeout_ms должен быть положительным")
    try:
        connection = _session_bus()
    except PortalError as exc:
        callback(None, str(exc))
        return

    handle_token = _new_handle_token()
    expected_path = _request_path(connection, handle_token)
    state: dict = {"done": False, "handle": expected_path}
    timer = GLib.timeout_source_new(timeout_ms)

    def close_request(handle):
        # Закрываем системный диалог при таймауте, не меняя разрешения.
        try:
            connection.call(
                PORTAL_BUS_NAME,
                handle,
                REQUEST_INTERFACE,
                "Close",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                1000,
                None,
                None,
                None,
            )
        except GLib.Error:
            pass

    def finish(path: Optional[str], error: Optional[str]) -> None:
        if state["done"]:
            return
        state["done"] = True
        timer.destroy()
        if state.get("subscription") is not None:
            connection.signal_unsubscribe(state["subscription"])
        callback(path, error)

    def on_response(_conn, _sender, _path, _iface, _signal, params, _data):
        response, results = params.unpack()
        if response != 0:
            finish(
                None,
                "захват отменён пользователем"
                if response == 1
                else "портал не смог выполнить захват",
            )
            return
        uri = results.get("uri")
        if not uri:
            finish(None, "портал вернул пустой ответ")
            return
        try:
            path = _uri_to_path(uri)
        except PortalError as exc:
            finish(None, str(exc))
            return
        if not os.path.isfile(path):
            finish(None, f"файл скриншота не найден: {path}")
            return
        finish(path, None)

    def on_timeout(*_args):
        state["timed_out"] = True
        close_request(state["handle"])
        finish(None, "портал не ответил вовремя")
        return GLib.SOURCE_REMOVE

    state["subscription"] = connection.signal_subscribe(
        PORTAL_BUS_NAME,
        REQUEST_INTERFACE,
        "Response",
        expected_path,
        None,
        Gio.DBusSignalFlags.NONE,
        on_response,
        None,
    )
    timer.set_callback(on_timeout)
    timer.attach(GLib.MainContext.get_thread_default() or GLib.MainContext.default())

    def on_call_done(source, result, _data):
        try:
            handle = source.call_finish(result).unpack()[0]
            if state["done"]:
                if state.get("timed_out"):
                    close_request(handle)
                return
            if handle != state["handle"]:
                connection.signal_unsubscribe(state["subscription"])
                state["handle"] = handle
                state["subscription"] = connection.signal_subscribe(
                    PORTAL_BUS_NAME,
                    REQUEST_INTERFACE,
                    "Response",
                    handle,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    on_response,
                    None,
                )
        except GLib.Error as exc:
            finish(None, f"портал отказал: {exc.message}")

    try:
        connection.call(
            PORTAL_BUS_NAME,
            PORTAL_OBJECT_PATH,
            SCREENSHOT_INTERFACE,
            "Screenshot",
            GLib.Variant("(sa{sv})", ("", _build_options(interactive, handle_token))),
            GLib.VariantType.new("(o)"),
            Gio.DBusCallFlags.NONE,
            timeout_ms,
            None,
            on_call_done,
            None,
        )
    except GLib.Error as exc:
        finish(None, f"не удалось отправить запрос: {exc.message}")
