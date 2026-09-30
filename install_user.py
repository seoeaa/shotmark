#!/usr/bin/env python3
"""Воспроизводимая пользовательская установка без sudo и автозапуска."""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import tempfile

from app_info import APP_ID

SOURCE = Path(__file__).resolve().parent
MARKER = "Managed by Shotmark installer"
FILES = (
    "app_info.py",
    "shotmark.py",
    "editor.py",
    "portal.py",
    "storage.py",
    "cairo_bootstrap.py",
    "tray.py",
    "README.md",
    "REVIEW.md",
    "LICENSE",
)


def locations(prefix):
    prefix = Path(prefix).expanduser().absolute()
    return (
        prefix / "share/shotmark",
        prefix / "bin/shotmark",
        prefix / f"share/applications/{APP_ID}.desktop",
    )


def _check_owned(package, launcher, desktop):
    for path in (package, launcher, desktop):
        if path.is_symlink():
            raise RuntimeError(f"Не заменяю символическую ссылку: {path}")
    if package.exists() and not (package / ".shotmark-managed").is_file():
        raise RuntimeError(f"Каталог не принадлежит установщику: {package}")
    for path in (launcher, desktop):
        if path.exists() and MARKER not in path.read_text():
            raise RuntimeError(f"Не заменяю посторонний файл: {path}")


def _atomic_text(path, text, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".shotmark-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def launcher_text(package):
    editor = shlex.quote(str(package / "shotmark.py"))
    tray = shlex.quote(str(package / "tray.py"))
    return f"""#!/bin/sh
# {MARKER}
case "${{1-}}" in
  --tray) shift; exec /usr/bin/python3 {tray} "$@" ;;
  --quit-tray) shift; exec /usr/bin/python3 {tray} --quit "$@" ;;
  *) exec /usr/bin/python3 {editor} "$@" ;;
esac
"""


def desktop_text(launcher, icon_path=None):
    # Desktop Entry Exec — не shell; путь заключается в двойные кавычки.
    escaped = str(launcher).replace("\\", "\\\\\\\\").replace('"', '\\\\"').replace("%", "%%")
    return f"""[Desktop Entry]
# {MARKER}
Type=Application
Name=Shotmark
Comment=Скриншоты и аннотации — меню в системном трее
Exec="{escaped}" --tray
Icon={icon_path or APP_ID}
Terminal=false
StartupNotify=false
Categories=Graphics;
Keywords=screenshot;capture;annotation;скриншот;
Actions=Screenshot;

[Desktop Action Screenshot]
Name=Сделать скриншот
Exec="{escaped}"
"""


def install(prefix=Path.home() / ".local"):
    package, launcher, desktop = locations(prefix)
    _check_owned(package, launcher, desktop)
    package.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".shotmark-stage-", dir=package.parent))
    backup = None
    try:
        for name in FILES:
            shutil.copy2(SOURCE / name, stage / name)
        shutil.copytree(SOURCE / "assets/icons", stage / "assets/icons")
        if (SOURCE / ".deps").is_dir():
            shutil.copytree(
                SOURCE / ".deps", stage / ".deps", ignore=shutil.ignore_patterns("__pycache__")
            )
        (stage / ".shotmark-managed").write_text(MARKER + "\n")
        # Проверяем зависимости именно копии, а не исходного каталога.
        subprocess.run(["/usr/bin/python3", str(stage / "shotmark.py"), "--version"], check=True)
        if package.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = package.with_name(f"shotmark-backup-{stamp}")
            package.rename(backup)
        try:
            stage.rename(package)
        except OSError:
            if backup is not None:
                backup.rename(package)
            raise
        _atomic_text(launcher, launcher_text(package), 0o755)
        icon = package / f"assets/icons/hicolor/256x256/apps/{APP_ID}.png"
        _atomic_text(desktop, desktop_text(launcher, icon), 0o644)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(desktop.parent)], check=False)
    return package, launcher, desktop, backup


def uninstall(prefix=Path.home() / ".local"):
    package, launcher, desktop = locations(prefix)
    _check_owned(package, launcher, desktop)
    # Сначала корректно закрываем только трей, а не редакторы с работой пользователя.
    if launcher.exists():
        subprocess.run([str(launcher), "--quit-tray"], check=False, timeout=5)
    for path in (launcher, desktop):
        path.unlink(missing_ok=True)
    if package.exists():
        shutil.rmtree(package)
    if shutil.which("update-desktop-database") and desktop.parent.exists():
        subprocess.run(["update-desktop-database", str(desktop.parent)], check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local")
    parser.add_argument("--uninstall", action="store_true")
    options = parser.parse_args()
    if options.uninstall:
        uninstall(options.prefix)
        print("Установка удалена; исходники, заметки и скриншоты сохранены.")
    else:
        package, launcher, desktop, backup = install(options.prefix)
        print(f"Приложение: {package}\nКоманда: {launcher}\nМеню приложений: {desktop}")
        if backup:
            print(f"Предыдущая версия: {backup}")
        print("Автозапуск при входе не добавлен. Запустить трей: shotmark --tray")


if __name__ == "__main__":
    main()
