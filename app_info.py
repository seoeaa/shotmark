"""Общие метаданные без импорта GTK: трей GTK3 и редактор GTK4."""
from pathlib import Path

APP_ID = "io.github.shotmark.Shotmark"
VERSION = "0.3.0"
ICON_NAME = APP_ID
ICON_ROOT = Path(__file__).resolve().parent / "assets" / "icons" / "hicolor"
