"""Связка pycairo и PyGObject.

GTK4 рисует через `cairo.Context`, и PyGObject должен уметь принимать
pycairo-объекты. Отвечает за это пакет `python3-gi-cairo`:

    sudo apt install python3-gi-cairo

Если пакета нет и прав на установку тоже нет, модуль подхватывает
локальную копию из каталога `.deps` рядом с проектом (см.
`bootstrap-deps.sh`). Версия копии обязана совпадать с системным
`python3-gi` — иначе ABI-конфликт.

Системный `gi` не заменяется и не перезагружается. Локальный путь
добавляется только для недостающего Cairo bridge после проверки ядра.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DEPS = os.path.join(_HERE, ".deps")

_MISSING_HELP = (
    "нет связи pycairo с PyGObject, поэтому GTK4 не сможет рисовать.\n"
    "Установите пакет:\n"
    "    sudo apt install python3-gi-cairo\n"
    "либо соберите локальную копию:\n"
    "    ./bootstrap-deps.sh"
)


def import_gi():
    """Использовать системный gi; при необходимости добавить только Cairo bridge.

    Не перезагружаем gi и не подменяем sys.path. Локальный bridge допустим,
    только если сохранённое ядро PyGObject побайтно совпадает с загруженным.
    После обновления системного пакета требуется пересборка .deps.
    """
    import gi

    try:
        gi.require_foreign("cairo")
        return gi
    except ImportError:
        pass

    from gi import _gi

    system_core = Path(_gi.__file__)
    local = Path(LOCAL_DEPS) / "gi"
    local_core = local / system_core.name
    try:
        compatible = (
            hashlib.sha256(system_core.read_bytes()).digest()
            == hashlib.sha256(local_core.read_bytes()).digest()
        )
    except OSError:
        compatible = False
    if not compatible:
        raise ImportError(f"{_MISSING_HELP}\nЛокальная копия отсутствует или устарела.")
    if str(local) not in gi.__path__:
        gi.__path__.append(str(local))
    try:
        gi.require_foreign("cairo")
    except ImportError as exc:
        raise ImportError(f"{_MISSING_HELP}\n(причина: {exc})") from exc
    return gi
