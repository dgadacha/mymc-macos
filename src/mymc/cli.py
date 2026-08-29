"""Command line interface for manipulating PS2 memory card images.

Python 3 port of ``mymc.py`` by Ross Ridge (public domain), rewritten on
top of :mod:`argparse`.
"""

import argparse
import os
import struct
import sys
import time
from errno import EEXIST

from . import __version__, ps2mc, ps2save
from .ps2mc_dir import (
    DF_EXECUTE,
    DF_EXISTS,
    DF_HIDDEN,
    DF_POCKETSTN,
    DF_PROTECTED,
    DF_PSX,
    DF_READ,
    DF_WRITE,
    mode_is_dir,
    mode_is_file,
    tod_to_time,
    zero_terminate,
)

PROG = "mymc"

io_error = ps2mc.io_error

SAVE_SUFFIXES = (".psu", ".max", ".cbs", ".sps", ".xps")


def _copy(fout, fin):
    """Copy the contents of one file to another."""
    while True:
        s = fin.read(65536)
        if not s:
            break
        fout.write(s)


def _glob(mc, args):
    """Expand card-side wildcards, leaving non-matching patterns alone."""
    ret = []
    for arg in args:
        match = mc.glob(arg)
        ret += match if match else [arg]
    return ret


#
# Commands
#


def do_ls(cmd, mc, opts):
    mode_bits = "rwxpfdD81C+KPH4"

    args = opts.directory or ["/"]
    out = sys.stdout
    args = _glob(mc, args)
    for dirname in args:
        d = mc.dir_open(dirname)
        try:
            if len(args) > 1:
                out.write("\n" + dirname + ":\n")
            for ent in d:
                mode = ent[0]
                if (mode & DF_EXISTS) == 0:
                    continue
                for bit in range(0, 15):
                    out.write(mode_bits[bit] if mode & (1 << bit) else "-")
                tod = ent[3] if opts.creation_time else ent[6]
                tm = time.localtime(tod_to_time(tod))
                out.write(
                    " %7d %04d-%02d-%02d %02d:%02d:%02d %s\n"
                    % (
                        ent[2], tm.tm_year, tm.tm_mon, tm.tm_mday,
                        tm.tm_hour, tm.tm_min, tm.tm_sec, ent[8],
                    )
                )
        finally:
            d.close()


def do_add(cmd, mc, opts):
    if opts.directory is not None:
        mc.chdir(opts.directory)
    for src in opts.filename:
        with open(src, "rb") as f:
            dest = os.path.basename(src)
            out = mc.open(dest, "wb")
            try:
                _copy(out, f)
            finally:
                out.close()


def do_extract(cmd, mc, opts):
    if opts.directory is not None:
        mc.chdir(opts.directory)

    close_out = False
    out = None
    if opts.output is not None:
        if opts.use_stdout:
            raise SystemExit(PROG + ": the -o and -p options are mutually exclusive.")
        out = open(opts.output, "wb")
        close_out = True
    elif opts.use_stdout:
        out = sys.stdout.buffer

    try:
        for filename in _glob(mc, opts.filename):
            f = mc.open(filename, "rb")
            try:
                if out is not None:
                    _copy(out, f)
                    continue
                name = filename.split("/")[-1]
                with open(name, "wb") as o:
                    _copy(o, f)
            finally:
                f.close()
    finally:
        if close_out:
            out.close()


def do_mkdir(cmd, mc, opts):
    for filename in opts.directory:
        mc.mkdir(filename)


def do_remove(cmd, mc, opts):
    for filename in opts.filename:
        mc.remove(filename)


def do_import(cmd, mc, opts):
    args = opts.savefile
    if opts.directory is not None and len(args) > 1:
        raise SystemExit(
            PROG + ": the -d option can only be used with a single save file."
        )

    for filename in args:
        with open(filename, "rb") as f:
            try:
                sf = ps2save.load_save_file(f)
            except ps2save.error as why:
                raise io_error(EEXIST, str(why), filename) from None
        dirname = opts.directory
        if dirname is None:
            dirname = sf.get_directory()[8]
        print("Importing %s to %s" % (filename, dirname))
        if not mc.import_save_file(sf, opts.ignore_existing, opts.directory):
            print(filename + ": already in memory card image, ignored.")


def do_export(cmd, mc, opts):
    if opts.overwrite_existing and opts.ignore_existing:
        raise SystemExit(PROG + ": the -i and -f options are mutually exclusive.")

    args = _glob(mc, opts.directory)
    if opts.output_file is not None:
        if len(args) > 1:
            raise SystemExit(
                PROG + ": only one directory can be exported with the -o option."
            )
        if opts.longnames:
            raise SystemExit(PROG + ": the -o and -l options are mutually exclusive.")

    for dirname in args:
        sf = mc.export_save_file(dirname, log=lambda m: print(m, file=sys.stderr))
        filename = opts.output_file
        if opts.longnames:
            filename = ps2save.make_longname(dirname, sf) + "." + opts.type
        if filename is None:
            filename = dirname + "." + opts.type
        if opts.into is not None:
            # Join rather than chdir: changing the working directory would
            # leak out of this call and surprise anything else in the process.
            filename = os.path.join(opts.into, filename)

        if not opts.overwrite_existing and os.path.exists(filename):
            if opts.ignore_existing:
                continue
            raise io_error(EEXIST, "File exists", filename)

        with open(filename, "wb") as f:
            print("Exporting %s to %s" % (dirname, filename))
            if opts.type == "max":
                sf.save_max_drive(f)
            else:
                sf.save_ems(f)


def do_delete(cmd, mc, opts):
    for dirname in opts.dirname:
        mc.rmdir(dirname)


def do_setmode(cmd, mc, opts):
    set_mask = 0
    clear_mask = ~0
    setting = cmd == "set"
    for (opt, bit) in [
        (opts.read, DF_READ),
        (opts.write, DF_WRITE),
        (opts.execute, DF_EXECUTE),
        (opts.protected, DF_PROTECTED),
        (opts.psx, DF_PSX),
        (opts.pocketstation, DF_POCKETSTN),
        (opts.hidden, DF_HIDDEN),
    ]:
        if opt:
            if setting:
                set_mask |= bit
            else:
                clear_mask ^= bit

    value = opts.hex_value
    if set_mask == 0 and clear_mask == ~0:
        if value is None:
            raise SystemExit(PROG + ": at least one option must be given.")
        value = int(value, 16)
    elif value is not None:
        raise SystemExit(PROG + ": the -X option can't be combined with other options.")

    for arg in _glob(mc, opts.filename):
        ent = mc.get_dirent(arg)
        if value is None:
            ent[0] = (ent[0] & clear_mask) | set_mask
        else:
            ent[0] = value
        mc.set_dirent(arg, ent)


def do_rename(cmd, mc, opts):
    mc.rename(opts.oldname, opts.newname)


def _get_ps2_title(mc, enc):
    s = mc.get_icon_sys(".")
    if s is None:
        return None
    return list(ps2save.icon_sys_title(ps2save.unpack_icon_sys(s), enc))


def _get_psx_title(mc, savename, enc):
    mode = mc.get_mode(savename)
    if mode is None or not mode_is_file(mode):
        return None
    f = mc.open(savename)
    try:
        s = f.read(128)
    finally:
        f.close()
    if len(s) != 128:
        return None
    (magic, icon, blocks, title) = struct.unpack("<2sBB64s28x32x", s)
    if magic != b"SC":
        return None
    return [ps2save.shift_jis_conv(zero_terminate(title), enc), ""]


def do_dir(cmd, mc, opts):
    enc = "ascii" if opts.ascii else None
    d = mc.dir_open("/")
    try:
        for ent in list(d)[2:]:
            dirmode = ent[0]
            if not mode_is_dir(dirmode):
                continue
            dirname = "/" + ent[8]
            mc.chdir(dirname)
            length = mc.dir_size(".")
            if dirmode & DF_PSX:
                title = _get_psx_title(mc, ent[8], enc)
            else:
                title = _get_ps2_title(mc, enc)
            if title is None:
                title = ["Corrupt", ""]
            protection = dirmode & (DF_PROTECTED | DF_WRITE)
            if protection == 0:
                protection = "Delete Protected"
            elif protection == DF_WRITE:
                protection = "Not Protected"
            elif protection == DF_PROTECTED:
                protection = "Copy & Delete Protected"
            else:
                protection = "Copy Protected"

            if dirmode & DF_PSX:
                protection = "PocketStation" if dirmode & DF_POCKETSTN else "PlayStation"

            print("%-32s %s" % (ent[8], title[0]))
            print("%4dKB %-25s %s" % (length // 1024, protection, title[1]))
            print()
    finally:
        d.close()

    print("{:,} KB Free".format(mc.get_free_space() // 1024))


def do_df(cmd, mc, opts):
    print("%s: %d bytes free." % (getattr(mc.f, "name", "image"), mc.get_free_space()))


def do_check(cmd, mc, opts):
    if mc.check():
        print("No errors found.")
        return 0
    return 1


def do_format(cmd, mcname, opts):
    pages_per_card = ps2mc.PS2MC_STANDARD_PAGES_PER_CARD
    if opts.clusters is not None:
        pages_per_cluster = (
            ps2mc.PS2MC_CLUSTER_SIZE // ps2mc.PS2MC_STANDARD_PAGE_SIZE
        )
        pages_per_card = opts.clusters * pages_per_cluster
    params = (
        not opts.no_ecc,
        ps2mc.PS2MC_STANDARD_PAGE_SIZE,
        ps2mc.PS2MC_STANDARD_PAGES_PER_ERASE_BLOCK,
        pages_per_card,
    )

    if not opts.overwrite_existing and os.path.exists(mcname):
        raise io_error(EEXIST, "file exists", mcname)

    with open(mcname, "w+b") as f:
        ps2mc.ps2mc(f, True, params).close()
    print("Formatted %s (%d KB)." % (mcname, pages_per_card * 512 // 1024))


def do_gui(cmd, mcname, opts):
    try:
        from .gui import run
    except ImportError as why:
        write_error(
            None,
            "GUI not available (%s).\n"
            "Install the GUI dependencies with:  pip install 'mymc-macos[gui]'" % why,
        )
        return 1
    return run(mcname)


#
# Debugging commands, only reachable with the global -D option.
#


def do_create_pad(cmd, mc, opts):
    length = mc.clusters_per_card
    if len(opts.args) > 1:
        length = int(opts.args[1])
    pad = b"\0" * mc.cluster_size
    f = mc.open(opts.args[0], "wb")
    try:
        for _ in range(length):
            f.write(pad)
    finally:
        f.close()


def do_frob(cmd, mc, opts):
    mc.write_superblock()


_trans = bytes.maketrans(bytes(range(32)), b" " * 32)


def _print_bin(base, s):
    for off in range(0, len(s), 16):
        a = s[off : off + 16]
        print(
            "%04X %s  %s"
            % (
                base + off,
                " ".join("%02X" % b for b in a).ljust(47),
                a.translate(_trans).decode("latin-1"),
            )
        )


def _print_erase_block(mc, n):
    ppb = mc.pages_per_erase_block
    base = n * ppb
    for i in range(ppb):
        _print_bin(i * mc.page_size, mc.read_page(base + i))
        print()


def do_print_good_blocks(cmd, mc, opts):
    print("good_block2:")
    _print_erase_block(mc, mc.good_block2)
    print("good_block1:")
    _print_erase_block(mc, mc.good_block1)


def do_ecc_check(cmd, mc, opts):
    for i in range(mc.clusters_per_card * mc.pages_per_cluster):
        try:
            mc.read_page(i)
        except ps2mc.ecc_error:
            print("bad: %05x" % i)


def write_error(filename, msg):
    if filename is None:
        sys.stderr.write(PROG + ": " + msg + "\n")
    else:
        sys.stderr.write(PROG + ": " + str(filename) + ": " + msg + "\n")


#
# Argument parsing
#


def build_parser(debug=False):
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Manipulate PlayStation 2 memory card images.",
        epilog="Run '%(prog)s IMAGE COMMAND --help' for help on a single command.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument(
        "-i", "--ignore-ecc", action="store_true",
        help="ignore ECC errors while reading",
    )
    parser.add_argument("-D", "--debug", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "image", metavar="IMAGE", nargs="?",
        help="the memory card image to work on (e.g. Mcd001.ps2)",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, func, help, mode):
        p = sub.add_parser(name, help=help, description=help)
        p.set_defaults(func=func, mode=mode)
        return p

    p = add("dir", do_dir, "Display save file information.", "rb")
    p.add_argument(
        "-a", "--ascii", action="store_true",
        help="transliterate Japanese titles to ASCII",
    )

    p = add("ls", do_ls, "List the contents of a directory.", "rb")
    p.add_argument("-c", "--creation-time", action="store_true",
                   help="display creation times")
    p.add_argument("directory", nargs="*", help="directories to list (default /)")

    p = add("extract", do_extract, "Extract files from the memory card.", "rb")
    p.add_argument("-o", "--output", metavar="FILE", help='extract file to "FILE"')
    p.add_argument("-d", "--directory", help='extract files from "DIRECTORY"')
    p.add_argument("-p", "--use-stdout", action="store_true",
                   help="extract files to standard output")
    p.add_argument("filename", nargs="+")

    p = add("add", do_add, "Add files to the memory card.", "r+b")
    p.add_argument("-d", "--directory", help='add files to "DIRECTORY"')
    p.add_argument("filename", nargs="+")

    p = add("mkdir", do_mkdir, "Make directories.", "r+b")
    p.add_argument("directory", nargs="+")

    p = add("remove", do_remove, "Remove files and directories.", "r+b")
    p.add_argument("filename", nargs="+")

    p = add("import", do_import, "Import save files into the memory card.", "r+b")
    p.add_argument("-i", "--ignore-existing", action="store_true",
                   help="ignore saves that already exist on the image")
    p.add_argument("-d", "--directory", metavar="DEST", help='import to "DEST"')
    p.add_argument("savefile", nargs="+",
                   help="save files (.psu, .max, .cbs, .sps, .xps)")

    p = add("export", do_export, "Export save files from the memory card.", "rb")
    p.add_argument("-f", "--overwrite-existing", action="store_true",
                   help="overwrite any save files already exported")
    p.add_argument("-i", "--ignore-existing", action="store_true",
                   help="ignore any save files already exported")
    p.add_argument("-o", "--output-file", metavar="FILENAME",
                   help='use "FILENAME" as the name of the save file')
    p.add_argument("-d", "--into", "--directory", metavar="DIRECTORY",
                   help='export save files into "DIRECTORY"')
    p.add_argument("-l", "--longnames", action="store_true",
                   help="generate longer, more descriptive filenames")
    p.add_argument("-p", "--ems", action="store_const", dest="type", const="psu",
                   default="psu", help="use the EMS .psu save file format [default]")
    p.add_argument("-m", "--max-drive", action="store_const", dest="type", const="max",
                   help="use the MAX Drive .max save file format")
    p.add_argument("directory", nargs="+", help="save directories to export")

    p = add("delete", do_delete,
            "Recursively delete a directory (save file).", "r+b")
    p.add_argument("dirname", nargs="+")

    for name, verb in (("set", "Set"), ("clear", "Clear")):
        p = add(name, do_setmode,
                "%s mode flags on files and directories." % verb, "r+b")
        p.add_argument("-p", "--protected", action="store_true",
                       help="%s copy protected flag" % verb.lower())
        p.add_argument("-P", "--psx", action="store_true",
                       help="%s PSX flag" % verb.lower())
        p.add_argument("-K", "--pocketstation", action="store_true",
                       help="%s PocketStation flag" % verb.lower())
        p.add_argument("-H", "--hidden", action="store_true",
                       help="%s hidden flag" % verb.lower())
        p.add_argument("-r", "--read", action="store_true",
                       help="%s read allowed flag" % verb.lower())
        p.add_argument("-w", "--write", action="store_true",
                       help="%s write allowed flag" % verb.lower())
        p.add_argument("-x", "--execute", action="store_true",
                       help="%s executable flag" % verb.lower())
        if name == "set":
            p.add_argument("-X", "--hex-value", metavar="MODE",
                           help='set the whole mode to "MODE" (hexadecimal)')
        else:
            p.add_argument("-X", dest="hex_value", default=None,
                           help=argparse.SUPPRESS)
        p.add_argument("filename", nargs="+")

    p = add("rename", do_rename, "Rename a file or directory.", "r+b")
    p.add_argument("oldname")
    p.add_argument("newname")

    add("df", do_df, "Display the amount of free space.", "rb")
    add("check", do_check, "Check for file system errors.", "rb")

    p = add("format", do_format, "Create a new memory card image.", None)
    p.add_argument("-c", "--clusters", type=int,
                   help="size of the memory card, in 1 KB clusters")
    p.add_argument("-f", "--overwrite-existing", action="store_true",
                   help="overwrite any existing file")
    p.add_argument("-e", "--no-ecc", action="store_true",
                   help="create an image without ECC")

    add("gui", do_gui, "Start the graphical user interface.", None)

    # Debugging commands, only registered with the global -D option so
    # that they stay out of --help.
    if debug:
        add("frob", do_frob, "Rewrite the superblock.", "r+b")
        add("print_good_blocks", do_print_good_blocks,
            "Hex dump the two spare erase blocks.", "rb")
        add("ecc_check", do_ecc_check, "Read every page, reporting ECC errors.", "rb")
        p = add("create_pad", do_create_pad, "Fill the card with a padding file.", "r+b")
        p.add_argument("args", nargs="+")

    return parser


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    debug = "-D" in argv or "--debug" in argv
    parser = build_parser(debug)
    opts = parser.parse_args(argv)

    if opts.command is None:
        # No command: start the GUI, on the image if one was named.
        return do_gui("gui", opts.image, opts)

    if opts.image is None:
        parser.error(
            "an image file is required:  %s IMAGE %s ..." % (PROG, opts.command)
        )

    mcname = opts.image
    f = None
    mc = None
    ret = 0

    try:
        try:
            if opts.mode is None:
                ret = opts.func(opts.command, mcname, opts)
            else:
                f = open(mcname, opts.mode)
                mc = ps2mc.ps2mc(f, opts.ignore_ecc)
                ret = opts.func(opts.command, mc, opts)
        finally:
            if mc is not None:
                mc.close()
            if f is not None:
                f.close()

    except (ps2mc.error, ps2save.error) as value:
        # io_error.__str__ already prefixes the filename; take the bare
        # message so it is not printed twice.
        write_error(
            getattr(value, "filename", None) or mcname,
            getattr(value, "strerror", None) or str(value),
        )
        if opts.debug:
            raise
        return 1

    except OSError as value:
        if getattr(value, "filename", None) is not None:
            write_error(value.filename, value.strerror)
        elif getattr(value, "strerror", None) is not None:
            write_error(mcname, value.strerror)
        else:
            raise
        if opts.debug:
            raise
        return 1

    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130

    return 0 if ret is None else ret


def main_gui(argv=None):
    """Entry point for the ``mymc-gui`` command."""
    args = sys.argv[1:] if argv is None else argv
    return do_gui("gui", args[0] if args else None, None)


if __name__ == "__main__":
    sys.exit(main())
