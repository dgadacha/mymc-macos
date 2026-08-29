# mymc for macOS

A utility for working with PlayStation 2 memory card images — the
`Mcd001.ps2` files PCSX2 uses. Import and export save games, list them,
delete them, create new cards.

This is a **Python 3** port of [mymc](https://www.csclub.uwaterloo.ca/~rridge/mymc/)
by Ross Ridge (public domain), with a **Qt** interface that runs natively
on macOS. The original was Python 2.7, wxPython, and two Windows DLLs:
`mymcsup.dll` for MAX Drive compression and `mymcicon.dll`, which drew
the 3D save icons with Direct3D. None of that worked on a Mac.

![mymc running on macOS](docs/screenshot.png)

The 3D save icons, as mymc renders them on macOS — geometry, texture and
lighting read straight off the card, drawn without a GPU:

![PS2 save icons rendered in software](docs/icons.png)

## What changed

| | mymc 2.7 (original) | this version |
|---|---|---|
| Python | 2.7 (end of life in 2020) | 3.9 → 3.14 |
| Interface | wxPython | PySide6 / Qt 6, dark mode, Retina |
| 3D icons | `mymcicon.dll`, Direct3D, Windows only | NumPy software renderer, everywhere |
| MAX Drive compression | `mymcsup.dll`, otherwise 100× slower | pure Python, NumPy-accelerated |
| ECC | Python loops | vectorised with NumPy (~50× faster) |
| Command line | `optparse` | `argparse`, subcommands, per-command `--help` |
| macOS integration | — | `.app` bundle, Finder double-click, drag and drop |

The images it produces are identical in format: every superblock field
matches a real PS2 card, and images are exactly 8,650,752 bytes like
PCSX2's own.

## Installation

You need Python 3.9 or newer. On a Mac with Homebrew:

```bash
brew install python
```

Then, from this directory:

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[gui]"
```

The `mymc` and `mymc-gui` commands are then in `.venv/bin/`. To have them
on your `PATH` everywhere, add that directory to it, or install with
[pipx](https://pipx.pypa.io/):

```bash
pipx install ".[gui]"
```

**Without the GUI**, the library and command line tool have *no*
dependencies at all:

```bash
pipx install .
```

NumPy is still worth having (`pip install ".[speed]"`) — it makes the ECC
calculation much faster. Everything works without it.

### macOS application

For a Dock icon, a proper menu bar and Finder file associations:

```bash
.venv/bin/python tools/make_app.py --output dist
```

Then drag `dist/mymc.app` into `/Applications`. Double-clicking a `.ps2`
opens it; double-clicking a `.psu` or `.max` save imports it into the
card you have open.

The bundle launches the interpreter mymc is installed in, so it is not
self-contained — don't hand it to someone else as is. For a
redistributable app, run PyInstaller or py2app against the same entry
point (`mymc.cli:main_gui`).

## Graphical interface

```bash
mymc-gui                    # or:  mymc Mcd001.ps2 gui
```

- a sortable list of saves, with title, size and date;
- a preview of the save's **animated 3D icon**: drag to spin it,
  double-click to recentre, right-click for lighting, camera and
  animation options;
- **drag and drop**: drop a card image on the window to open it, or a
  `.psu`, `.max`, `.cbs`, `.sps` or `.xps` to import it. The type is
  recognised from the file's header rather than its name, so a card
  called `Mcd001.bin`, `.mcd`, `.mcr` or with no extension at all opens
  just the same;
- export to `.psu` (EMS) or `.max` (MAX Drive), with a progress bar;
- free space shown in the status bar at all times.

Japanese titles are displayed as they are stored; *View ▸ Transliterate
Japanese Titles* converts them to lookalike Latin characters
(`ＤＡＴＡ` → `DATA`, `【あ】` → `[あ]`).

## Command line

The shape of it is `mymc IMAGE COMMAND [options]`.

```bash
# create an 8 MB card
mymc Mcd001.ps2 format

# see what is on it, the way the PS2 browser would show it
mymc Mcd001.ps2 dir

# import saves (the format is detected automatically)
mymc Mcd001.ps2 import ~/Downloads/*.psu ~/Downloads/*.max

# export, with a descriptive filename
mymc Mcd001.ps2 export -l BASLUS-20678SAVE
# → "SLUS-20678 UNLIMITED SAGA SYSTEMDATA (9AA6AB3E).psu"

# export in the MAX Drive format
mymc Mcd001.ps2 export -m BASLUS-20678SAVE

# delete a save, check the file system
mymc Mcd001.ps2 delete BASLUS-20678SAVE
mymc Mcd001.ps2 check
```

Available commands: `dir`, `ls`, `add`, `extract`, `mkdir`, `remove`,
`import`, `export`, `delete`, `set`, `clear`, `rename`, `df`, `check`,
`format`, `gui`. Each has its own help:

```bash
mymc Mcd001.ps2 export --help
```

### Save file formats

| Format | Extension | Read | Write |
|---|---|:---:|:---:|
| EMS | `.psu` | yes | yes |
| MAX Drive | `.max` | yes | yes |
| Code Breaker | `.cbs` | yes | — |
| SharkPort / X-Port | `.sps`, `.xps` | yes | — |
| nPort | `.npo` | — | — |

## Using it as a library

```python
from mymc import ps2mc, ps2save

with open("Mcd001.ps2", "r+b") as f:
    with ps2mc.ps2mc(f) as mc:
        print(mc.get_free_space() // 1024, "KB free")

        with open("save.psu", "rb") as g:
            mc.import_save_file(ps2save.load_save_file(g), ignore_existing=True)

        sf = mc.export_save_file("/BASLUS-20678SAVE")
        with open("copy.max", "wb") as g:
            sf.save_max_drive(g)
```

Render a save's 3D icon to a PNG, with no GUI involved:

```python
from mymc import ps2icon, render

icon = ps2icon.parse_icon(open("list.icn", "rb").read())
render.render_to_png(icon, "icon.png", size=256, angle=0.6)
```

## Tested on real cards

Tested against the PCSX2 cards in `~/Library/Application Support/PCSX2/memcards`,
holding saves from Kingdom Hearts, Gran Turismo 4, Batman Begins,
Tekken 5, Burnout 3 and Need for Speed Most Wanted:

- reading the directory, the titles (full-width Japanese included) and
  the file system check: no errors;
- **the games' 3D icons display**, textured and lit as they are in the
  PS2's own memory card browser;
- every save survives card → `.psu` → `.max` → card with identical
  SHA-256 digests.

A card PCSX2 has created but no game has formatted yet is all `0xFF`;
mymc says so and points at the format command instead of calling it
unreadable.

One thing is slow: MAX Drive compression is pure Python. A 130 KB save
goes through in a fraction of a second, but Gran Turismo 4's 1.5 MB of
game data takes about twenty seconds — for no gain, since that data is
already incompressible. The interface shows a progress bar and stays
responsive. The default `.psu` format is instant.

## Before you use it

- **Do not modify an image while PCSX2 has it open.** The emulator keeps
  the card in memory and will write over your changes, or corrupt it.
  Quit PCSX2 first.
- Back up your images before writing to them. The original described
  itself as "alpha" quality; this port has tests behind it, but caution
  is still warranted with data you care about.
- The bad block list is ignored, as in the original. That has no effect
  on images made by PCSX2 or by mymc, which have none.

## Development

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest          # 101 tests
```

The tests cover LZARI compression round trips, ECC correction (compared
bit for bit against the original algorithm), the file system, all four
save formats, icon rendering, drag and drop, and the command line end to
end. The interface tests run without a display, through Qt's offscreen
platform plugin.

Layout:

```
src/mymc/
    ps2mc.py        the card's file system
    ps2mc_dir.py    directory entries
    ps2mc_ecc.py    Hamming codes (ECC)
    ps2save.py      the .psu / .max / .cbs / .sps formats
    lzari.py        LZARI codec (MAX Drive compression)
    ps2icon.py      the .icn 3D icon format
    render.py       software rasteriser for the icons
    cli.py          command line
    gui/            Qt interface
```

### Porting notes

Three things needed care:

- **`bytes` versus `str`.** File contents are `bytes` throughout;
  directory entry names are exposed as `str` through `latin-1`, which
  maps bytes 0–255 one to one onto the first 256 code points, so the
  round trip stays exact even for a non-ASCII name.
- **Division.** The LZARI arithmetic coder depends on truncating integer
  division; all 16 divisions in the original module were converted one by
  one to floor division (`//`).
- **ECC.** The port was compared against a literal transcription of the
  Python 2 algorithm over 500 random error patterns: no divergence.
  Single bit errors are corrected, two bit errors detected.

The icon renderer had no original to compare against. The first version
drew everything **mirrored** — "KINGDOM HEARTS" read backwards — because
the view basis used `cross(forward, up)` instead of `cross(up, forward)`.
Two tests pin it down now: a triangle on the model's +X side must land on
the right of the frame, and one at +Y — PS2 icon space points Y down —
at the bottom.

## Licence

Public domain, like the original mymc. See `LICENSE.txt`.

mymc was written by Ross Ridge. The LZARI algorithm is Haruhiko
Okumura's.
