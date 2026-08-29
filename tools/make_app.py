#!/usr/bin/env python3
"""Build a macOS ``mymc.app`` wrapper around the installed package.

This produces a small bundle that launches the interpreter mymc is
installed in, so it is not a self-contained, redistributable app -- it is
the thing you drag into /Applications on your own machine to get a Dock
icon, a proper menu bar and Finder file associations.

    python tools/make_app.py [--output DIR] [--python PATH]

For a standalone bundle to hand to someone else, use PyInstaller or
py2app on top of the same entry point (``mymc.cli:main_gui``).
"""

import argparse
import os
import plistlib
import shutil
import subprocess
import sys

BUNDLE_ID = "org.publicdomain.mymc"

ICON_SIZES = [
    ("icon_16x16", 16), ("icon_16x16@2x", 32),
    ("icon_32x32", 32), ("icon_32x32@2x", 64),
    ("icon_128x128", 128), ("icon_128x128@2x", 256),
    ("icon_256x256", 256), ("icon_256x256@2x", 512),
    ("icon_512x512", 512), ("icon_512x512@2x", 1024),
]

CARD_EXTENSIONS = ["ps2", "mcd", "mc2", "mcr", "bin"]
SAVE_EXTENSIONS = ["psu", "max", "cbs", "sps", "xps"]

LAUNCHER = """#!/bin/sh
# Launch mymc's Qt interface using the interpreter it is installed in.
exec {python} -c 'import sys; from mymc.cli import main_gui; sys.exit(main_gui())' "$@"
"""


def build_icns(target_dir):
    """Render the app icon and convert it to an .icns with iconutil."""
    try:
        from PySide6.QtGui import QPainter, QPixmap
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("PySide6 is not installed; skipping the icon.", file=sys.stderr)
        return None

    from mymc.gui.appicon import _draw_card

    app = QApplication.instance() or QApplication(sys.argv[:1])
    iconset = os.path.join(target_dir, "mymc.iconset")
    os.makedirs(iconset, exist_ok=True)
    for name, size in ICON_SIZES:
        pixmap = QPixmap(size, size)
        pixmap.fill(0)  # transparent
        painter = QPainter(pixmap)
        _draw_card(painter, size)
        painter.end()
        pixmap.save(os.path.join(iconset, name + ".png"), "PNG")

    icns = os.path.join(target_dir, "mymc.icns")
    try:
        subprocess.run(
            ["iconutil", "-c", "icns", iconset, "-o", icns],
            check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as why:
        print("iconutil failed (%s); the app will use a default icon." % why,
              file=sys.stderr)
        return None
    finally:
        shutil.rmtree(iconset, ignore_errors=True)
    return icns


def document_types():
    return [
        {
            "CFBundleTypeName": "PS2 Memory Card Image",
            "CFBundleTypeExtensions": CARD_EXTENSIONS,
            "CFBundleTypeRole": "Editor",
            "LSHandlerRank": "Alternate",
        },
        {
            "CFBundleTypeName": "PS2 Save File",
            "CFBundleTypeExtensions": SAVE_EXTENSIONS,
            "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "Owner",
        },
    ]


def build(output_dir, python):
    from mymc import __version__

    app = os.path.join(output_dir, "mymc.app")
    contents = os.path.join(app, "Contents")
    macos = os.path.join(contents, "MacOS")
    resources = os.path.join(contents, "Resources")

    if os.path.exists(app):
        shutil.rmtree(app)
    os.makedirs(macos)
    os.makedirs(resources)

    launcher = os.path.join(macos, "mymc")
    with open(launcher, "w") as f:
        f.write(LAUNCHER.format(python=shell_quote(python)))
    os.chmod(launcher, 0o755)

    info = {
        "CFBundleName": "mymc",
        "CFBundleDisplayName": "mymc",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "mymc",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": __version__,
        "CFBundleShortVersionString": __version__,
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        # let the app follow the system light/dark appearance
        "NSRequiresAquaSystemAppearance": False,
        "CFBundleDocumentTypes": document_types(),
        "NSHumanReadableCopyright": "Public domain.",
    }

    icns = build_icns(resources)
    if icns:
        info["CFBundleIconFile"] = "mymc"

    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        plistlib.dump(info, f)

    return app


def shell_quote(path):
    return "'" + path.replace("'", "'\\''") + "'"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-o", "--output", default=".",
        help="directory to create mymc.app in (default: current directory)",
    )
    parser.add_argument(
        "-p", "--python", default=sys.executable,
        help="the interpreter mymc is installed in (default: this one)",
    )
    args = parser.parse_args()

    app = build(os.path.abspath(args.output), args.python)
    print("Created " + app)
    print("Drag it into /Applications, or run:  open " + shell_quote(app))


if __name__ == "__main__":
    main()
