"""Атомарный экспорт без перезаписи существующих файлов и удаления исходника."""
from pathlib import Path
import os
import shutil
import tempfile


def _publish(path, write, *, unique=False):
    target = Path(path).expanduser().absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".shotmark-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        candidate = target
        index = 0
        while True:
            try:
                # link публикует готовый файл атомарно и никогда не заменяет цель.
                os.link(temporary, candidate)
                return str(candidate)
            except FileExistsError:
                if not unique:
                    raise
                index += 1
                candidate = target.with_name(f"{target.stem}-{index}{target.suffix}")
    finally:
        os.unlink(temporary)


def save_png(surface, path, *, unique=False):
    return _publish(path, surface.write_to_png, unique=unique)


def copy_capture(source, target):
    """Исходник принадлежит порталу/пользователю: не перемещаем и не удаляем."""

    def write(stream):
        with open(source, "rb") as original:
            shutil.copyfileobj(original, stream)

    return _publish(target, write)
