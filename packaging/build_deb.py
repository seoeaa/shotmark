#!/usr/bin/env python3
"""Build a native .deb from an explicit allowlist, without root or network access."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_info import APP_ID, VERSION
from install_user import FILES, desktop_text, launcher_text


DEPENDENCIES = (
    "python3 (>= 3.10), python3-gi, python3-cairo, python3-gi-cairo, "
    "gir1.2-gtk-4.0 (>= 4.10), gir1.2-gtk-3.0, "
    "gir1.2-ayatanaappindicator3-0.1, xdg-desktop-portal"
)


def source_epoch():
    value = os.environ.get("SOURCE_DATE_EPOCH")
    if value is None:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "log", "-1", "--format=%ct"], capture_output=True, text=True
        )
        value = result.stdout.strip() if result.returncode == 0 else "0"
    return int(value)


def build(output_dir):
    if not re.fullmatch(r"\d+\.\d+\.\d+", VERSION):
        raise ValueError("VERSION must be a numeric major.minor.patch release")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f"shotmark_{VERSION}_all.deb"
    epoch = source_epoch()
    with tempfile.TemporaryDirectory(prefix="shotmark-deb-") as temporary:
        tree = Path(temporary) / "root"
        app = tree / "usr/share/shotmark"
        app.mkdir(parents=True)
        for filename in FILES:
            shutil.copyfile(ROOT / filename, app / filename)
        shutil.copytree(ROOT / "assets/icons", app / "assets/icons")
        # hicolor/index.theme belongs to hicolor-icon-theme, not this package.
        for icon in (ROOT / "assets/icons/hicolor").rglob("*"):
            if icon.is_file() and icon.suffix in (".png", ".svg"):
                target = (
                    tree
                    / "usr/share/icons/hicolor"
                    / icon.relative_to(ROOT / "assets/icons/hicolor")
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(icon, target)
        launcher = tree / "usr/bin/shotmark"
        launcher.parent.mkdir(parents=True)
        launcher.write_text(launcher_text(Path("/usr/share/shotmark")))
        desktop = tree / f"usr/share/applications/{APP_ID}.desktop"
        desktop.parent.mkdir(parents=True)
        desktop.write_text(desktop_text(Path("/usr/bin/shotmark")))
        doc = tree / "usr/share/doc/shotmark"
        doc.mkdir(parents=True)
        shutil.copyfile(ROOT / "LICENSE", doc / "copyright")
        control = tree / "DEBIAN"
        control.mkdir()
        size = sum(path.stat().st_size for path in (tree / "usr").rglob("*") if path.is_file())
        (control / "control").write_text(
            f"Package: shotmark\nVersion: {VERSION}\nSection: graphics\nPriority: optional\n"
            "Architecture: all\nMaintainer: Shotmark contributors <seoeaa@users.noreply.github.com>\n"
            f"Installed-Size: {(size + 1023) // 1024}\nDepends: {DEPENDENCIES}\n"
            "Recommends: xdg-desktop-portal-gnome | xdg-desktop-portal-gtk | xdg-desktop-portal-kde\n"
            "Suggests: gnome-shell-extension-appindicator\n"
            "Homepage: https://github.com/seoeaa/shotmark\n"
            "Description: Screenshot annotation tool with a system tray for Linux\n"
            " Capture through xdg-desktop-portal and annotate with GTK4/Cairo.\n"
            " Includes arrows, shapes, text, mosaic, PNG export and a GTK3 Ayatana tray.\n"
        )
        checksums = []
        for path in sorted((tree / "usr").rglob("*")):
            if path.is_file():
                checksums.append(
                    f"{hashlib.md5(path.read_bytes()).hexdigest()}  {path.relative_to(tree)}"
                )
        (control / "md5sums").write_text("\n".join(checksums) + "\n")
        # Source checkout permissions (including 0600) must not leak into the package.
        for path in [tree, *tree.rglob("*")]:
            path.chmod(0o755 if path.is_dir() or path == launcher else 0o644)
            os.utime(path, (epoch, epoch))
        subprocess.run(
            [
                "dpkg-deb",
                "--root-owner-group",
                "--uniform-compression",
                "-Zxz",
                "--build",
                str(tree),
                str(artifact),
            ],
            check=True,
            env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch)},
        )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(f"{digest}  {artifact.name}\n")
    print(
        f"Built {artifact} (source epoch {datetime.fromtimestamp(epoch, timezone.utc).isoformat()})"
    )
    return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    options = parser.parse_args()
    build(options.output_dir)


if __name__ == "__main__":
    main()
