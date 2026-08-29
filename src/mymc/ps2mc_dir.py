"""Directory entries of a PlayStation 2 memory card.

Python 3 port of ``ps2mc_dir.py`` by Ross Ridge (public domain).

A directory entry is represented as a 9 element list:

===  =====================================================================
  0  mode bits (``DF_*``)
  1  unused
  2  length: file size in bytes, or number of entries for a directory
  3  creation time, as a "ToD" tuple ``(sec, min, hour, mday, month, year)``
  4  first FAT cluster of the entry's data
  5  index of the entry within its parent directory
  6  modification time, as a ToD tuple
  7  attributes (unused by the PS2 browser)
  8  name, as ``str``
===  =====================================================================

Names are stored on the card as at most 32 raw bytes.  They are exposed as
``str`` and translated with ``latin-1``, which maps bytes 0-255 one to one
onto the first 256 code points; the round trip is therefore lossless even
for the rare card that holds a non-ASCII name.
"""

import calendar
import os
import struct
import time

PS2MC_DIRENT_LENGTH = 512

#: Encoding used to move directory entry names between ``bytes`` and ``str``.
NAME_ENCODING = "latin-1"

DF_READ = 0x0001
DF_WRITE = 0x0002
DF_EXECUTE = 0x0004
DF_RWX = DF_READ | DF_WRITE | DF_EXECUTE
DF_PROTECTED = 0x0008
DF_FILE = 0x0010
DF_DIR = 0x0020
DF_O_DCREAT = 0x0040
DF_0080 = 0x0080
DF_0100 = 0x0100
DF_O_CREAT = 0x0200
DF_0400 = 0x0400
DF_POCKETSTN = 0x0800
DF_PSX = 0x1000
DF_HIDDEN = 0x2000
DF_4000 = 0x4000
DF_EXISTS = 0x8000


def zero_terminate(s: bytes) -> bytes:
    """Truncate a byte string at the first NUL, if any."""
    i = s.find(b"\0")
    if i == -1:
        return s
    return s[:i]


def decode_name(s: bytes) -> str:
    """Decode a raw, NUL padded directory entry name."""
    return zero_terminate(s).decode(NAME_ENCODING, "replace")


def encode_name(s) -> bytes:
    """Encode a directory entry name back to its raw form."""
    if isinstance(s, (bytes, bytearray)):
        return bytes(s)
    return s.encode(NAME_ENCODING, "replace")


# mode, ???, length, created,
# fat_cluster, parent_entry, modified, attr,
# name
_dirent_struct = struct.Struct("<HHL8sLL8sL28x448s")

# secs, mins, hours, mday, month, year
_tod_struct = struct.Struct("<xBBBBBH")


def unpack_tod(s: bytes) -> tuple:
    """Unpack a raw 8 byte time-of-day field."""
    return _tod_struct.unpack(s)


def pack_tod(tod) -> bytes:
    """Pack a ToD tuple into its raw 8 byte form."""
    return _tod_struct.pack(*tod)


def unpack_dirent(s: bytes) -> list:
    """Unpack a raw 512 byte directory entry into a list."""
    ent = list(_dirent_struct.unpack(s))
    ent[3] = _tod_struct.unpack(ent[3])
    ent[6] = _tod_struct.unpack(ent[6])
    ent[8] = decode_name(ent[8])
    return ent


def pack_dirent(ent) -> bytes:
    """Pack a directory entry list into its raw 512 byte form."""
    ent = list(ent)
    ent[3] = _tod_struct.pack(*ent[3])
    ent[6] = _tod_struct.pack(*ent[6])
    ent[8] = encode_name(ent[8])[:32]
    return _dirent_struct.pack(*ent)


def time_to_tod(when: float) -> tuple:
    """Convert a Python time value to a ToD tuple.

    Timestamps on a memory card are kept in JST (UTC+9), the console's
    native time zone.
    """
    tm = time.gmtime(when + 9 * 3600)
    return (tm.tm_sec, tm.tm_min, tm.tm_hour, tm.tm_mday, tm.tm_mon, tm.tm_year)


def tod_to_time(tod) -> float:
    """Convert a ToD tuple to a Python time value."""
    try:
        month = tod[4]
        if month == 0:
            month = 1
        return (
            calendar.timegm((tod[5], month, tod[3], tod[2], tod[1], tod[0], 0, 1, 0))
            - 9 * 3600
        )
    except ValueError:
        return 0


def tod_now() -> tuple:
    """Get the current time as a ToD tuple."""
    return time_to_tod(time.time())


def tod_from_file(filename) -> tuple:
    """Get a file's modification time as a ToD tuple."""
    return time_to_tod(os.stat(filename).st_mtime)


def mode_is_file(mode: int) -> bool:
    return (mode & (DF_FILE | DF_DIR | DF_EXISTS)) == (DF_FILE | DF_EXISTS)


def mode_is_dir(mode: int) -> bool:
    return (mode & (DF_FILE | DF_DIR | DF_EXISTS)) == (DF_DIR | DF_EXISTS)
