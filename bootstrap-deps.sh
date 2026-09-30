#!/usr/bin/env bash
# Собирает локальную копию PyGObject с поддержкой Cairo в каталог .deps.
#
# Нужен только там, где нельзя поставить пакет:
#     sudo apt install python3-gi-cairo
# Root не требуется: deb-пакет скачивается в /tmp и распаковывается.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
DEPS="$ROOT/.deps"
WORK="$(mktemp -d)"
STAGE="$(mktemp -d "$ROOT/.deps-stage.XXXXXX")"
trap 'rm -rf "$WORK" "$STAGE"' EXIT

if ls /usr/lib/python3/dist-packages/gi/_gi_cairo*.so >/dev/null 2>&1; then
    echo "Системный python3-gi-cairo уже установлен — локальная копия не нужна."
    echo "Приложение автоматически предпочитает системный пакет."
    exit 0
fi

GI_SRC="$(env -u PYTHONPATH /usr/bin/python3 -c 'import gi, os; print(os.path.dirname(gi.__file__))')"
GI_VERSION="$(dpkg-query -W -f='${Version}' python3-gi)"
echo "Системный gi:  $GI_SRC"
echo "Собираю копию в: $DEPS"

cd "$WORK"
# Не брать произвольную новую версию моста к старому системному gi.
apt-get download "python3-gi-cairo=$GI_VERSION" >/dev/null
dpkg -x python3-gi-cairo_*.deb extracted

cp -r "$GI_SRC" "$STAGE/gi"
cp extracted/usr/lib/python3/dist-packages/gi/_gi_cairo*.so "$STAGE/gi/"

echo "Проверка временной сборки:"
PYTHONPATH="$STAGE" /usr/bin/python3 - <<'PY'
import gi
gi.require_foreign("cairo")
print("  cairo foreign: OK, gi", gi.__version__)
PY

# Существующая сборка сохраняется до успешной проверки новой.
if [[ -e "$DEPS" || -L "$DEPS" ]]; then
    mkdir -p "$ROOT/.review-backups"
    BACKUP="$(mktemp -d "$ROOT/.review-backups/deps.XXXXXX")"
    mv "$DEPS" "$BACKUP/previous"
    if ! mv "$STAGE" "$DEPS"; then
        mv "$BACKUP/previous" "$DEPS"
        exit 1
    fi
    echo "Предыдущая сборка: $BACKUP/previous"
else
    mv "$STAGE" "$DEPS"
fi
echo "Готово: $DEPS"
