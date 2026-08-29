"""Reading and writing the various PlayStation 2 save file formats.

Python 3 port of ``ps2save.py`` by Ross Ridge (public domain).

EMS (``.psu``) and MAX Drive (``.max``) saves can be both read and
written; SharkPort/X-Port (``.sps``, ``.xps``) and Code Breaker
(``.cbs``) saves can only be read.

Raw file contents are ``bytes``.  Names and titles are ``str``: names go
through the lossless ``latin-1`` mapping used by :mod:`mymc.ps2mc_dir`,
while ``icon.sys`` titles are decoded from Shift-JIS.
"""

import array
import binascii
import os
import struct
import sys
import zlib

from . import lzari
from .ps2mc_dir import (
    DF_0400,
    DF_DIR,
    DF_EXISTS,
    DF_FILE,
    DF_RWX,
    PS2MC_DIRENT_LENGTH,
    decode_name,
    encode_name,
    mode_is_dir,
    mode_is_file,
    pack_dirent,
    tod_now,
    tod_to_time,
    unpack_dirent,
    unpack_tod,
    zero_terminate,
)
from .rounding import round_up
from .sjistab import shift_jis_normalize_table

PS2SAVE_MAX_MAGIC = b"Ps2PowerSave"
PS2SAVE_SPS_MAGIC = b"\x0d\0\0\0SharkPortSave"
PS2SAVE_CBS_MAGIC = b"CFU\0"
PS2SAVE_NPO_MAGIC = b"nPort"

ICON_SYS_LENGTH = 964

# This is the initial permutation state ("S") for the RC4 stream cipher
# algorithm used to encrypt and decrypt Codebreaker saves.
PS2SAVE_CBS_RC4S = [
    0x5F, 0x1F, 0x85, 0x6F, 0x31, 0xAA, 0x3B, 0x18,
    0x21, 0xB9, 0xCE, 0x1C, 0x07, 0x4C, 0x9C, 0xB4,
    0x81, 0xB8, 0xEF, 0x98, 0x59, 0xAE, 0xF9, 0x26,
    0xE3, 0x80, 0xA3, 0x29, 0x2D, 0x73, 0x51, 0x62,
    0x7C, 0x64, 0x46, 0xF4, 0x34, 0x1A, 0xF6, 0xE1,
    0xBA, 0x3A, 0x0D, 0x82, 0x79, 0x0A, 0x5C, 0x16,
    0x71, 0x49, 0x8E, 0xAC, 0x8C, 0x9F, 0x35, 0x19,
    0x45, 0x94, 0x3F, 0x56, 0x0C, 0x91, 0x00, 0x0B,
    0xD7, 0xB0, 0xDD, 0x39, 0x66, 0xA1, 0x76, 0x52,
    0x13, 0x57, 0xF3, 0xBB, 0x4E, 0xE5, 0xDC, 0xF0,
    0x65, 0x84, 0xB2, 0xD6, 0xDF, 0x15, 0x3C, 0x63,
    0x1D, 0x89, 0x14, 0xBD, 0xD2, 0x36, 0xFE, 0xB1,
    0xCA, 0x8B, 0xA4, 0xC6, 0x9E, 0x67, 0x47, 0x37,
    0x42, 0x6D, 0x6A, 0x03, 0x92, 0x70, 0x05, 0x7D,
    0x96, 0x2F, 0x40, 0x90, 0xC4, 0xF1, 0x3E, 0x3D,
    0x01, 0xF7, 0x68, 0x1E, 0xC3, 0xFC, 0x72, 0xB5,
    0x54, 0xCF, 0xE7, 0x41, 0xE4, 0x4D, 0x83, 0x55,
    0x12, 0x22, 0x09, 0x78, 0xFA, 0xDE, 0xA7, 0x06,
    0x08, 0x23, 0xBF, 0x0F, 0xCC, 0xC1, 0x97, 0x61,
    0xC5, 0x4A, 0xE6, 0xA0, 0x11, 0xC2, 0xEA, 0x74,
    0x02, 0x87, 0xD5, 0xD1, 0x9D, 0xB7, 0x7E, 0x38,
    0x60, 0x53, 0x95, 0x8D, 0x25, 0x77, 0x10, 0x5E,
    0x9B, 0x7F, 0xD8, 0x6E, 0xDA, 0xA2, 0x2E, 0x20,
    0x4F, 0xCD, 0x8F, 0xCB, 0xBE, 0x5A, 0xE0, 0xED,
    0x2C, 0x9A, 0xD4, 0xE2, 0xAF, 0xD0, 0xA9, 0xE8,
    0xAD, 0x7A, 0xBC, 0xA8, 0xF2, 0xEE, 0xEB, 0xF5,
    0xA6, 0x99, 0x28, 0x24, 0x6C, 0x2B, 0x75, 0x5D,
    0xF8, 0xD3, 0x86, 0x17, 0xFB, 0xC0, 0x7B, 0xB3,
    0x58, 0xDB, 0xC7, 0x4B, 0xFF, 0x04, 0x50, 0xE9,
    0x88, 0x69, 0xC9, 0x2A, 0xAB, 0xFD, 0x5B, 0x1B,
    0x8A, 0xD9, 0xEC, 0x27, 0x44, 0x0E, 0x33, 0xC8,
    0x6B, 0x93, 0x32, 0x48, 0xB6, 0x30, 0x43, 0xA5,
]


class error(Exception):
    """Base for all exceptions specific to this module."""


class corrupt(error):
    """Corrupt save file."""

    def __init__(self, msg, f=None):
        self.filename = getattr(f, "name", None) if f is not None else None
        error.__init__(self, "Corrupt save file: " + msg)


class eof(corrupt):
    """Save file is truncated."""

    def __init__(self, f=None):
        corrupt.__init__(self, "Unexpected EOF", f)


class subdir(corrupt):
    """Save file contains something other than a plain file."""

    def __init__(self, f=None):
        corrupt.__init__(self, "Non-file in save file.", f)


#
# Table of graphically similar ASCII characters that can be used
# as substitutes for Unicode characters.
#
char_substs = {
    "¢": "c",
    "´": "'",
    "×": "x",
    "÷": "/",
    "‐": "-",
    "―": "-",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "′": "'",
    "−": "-",
    "≪": "<<",
    "≫": ">>",
    "─": "-",
    "━": "-",
    "│": "|",
    "┃": "|",
    "┌": "+",
    "┏": "+",
    "┐": "+",
    "┓": "+",
    "└": "+",
    "┗": "+",
    "┘": "+",
    "┛": "+",
    "├": "+",
    "┝": "+",
    "┠": "+",
    "┣": "+",
    "┤": "+",
    "┥": "+",
    "┨": "+",
    "┫": "+",
    "┬": "+",
    "┯": "+",
    "┰": "+",
    "┳": "+",
    "┷": "+",
    "┸": "+",
    "┻": "+",
    "┼": "+",
    "┿": "+",
    "╂": "+",
    "╋": "+",
    "■": "#",
    "□": "#",
    "★": "*",
    "☆": "*",
    "、": ",",
    "。": ".",
    "〃": '"',
    "〇": "0",
    "〈": "<",
    "〉": ">",
    "《": "<<",
    "》": ">>",
    "「": "[",
    "」": "]",
    "『": "[",
    "』": "]",
    "【": "[",
    "】": "]",
    "〔": "[",
    "〕": "]",
    "〜": "~",
    "ー": "-",
}


def shift_jis_conv(src, encoding=None) -> str:
    """Decode a Shift-JIS string, optionally down to a simpler encoding.

    ``src`` is the raw Shift-JIS ``bytes`` stored on the card.  The result
    is always a ``str``.  When ``encoding`` is given (say ``"ascii"``),
    characters the target encoding cannot represent are replaced by
    graphically similar ones, so that a Japanese title still reads as
    something rather than as a row of question marks -- full width
    ``ＤＡＴＡ`` becomes ``DATA``, ``【`` becomes ``[``, and so on.

    Characters with no similar-looking substitute are left as they are;
    a caller that really needs bytes in ``encoding`` should finish with
    ``.encode(encoding, "replace")`` itself.  On macOS this means export
    filenames keep their kana instead of turning into question marks.
    """
    if isinstance(src, str):
        u = src
    else:
        u = src.decode("shift_jis", "replace")
    if encoding in (None, "unicode", "utf-8", "utf8"):
        return u
    out = []
    for uc in u:
        try:
            uc.encode(encoding)
            out.append(uc)
        except UnicodeError:
            for uc2 in shift_jis_normalize_table.get(uc, uc):
                out.append(char_substs.get(uc2, uc2))
    return "".join(out)


def rc4_crypt(s, t) -> bytearray:
    """RC4 encrypt/decrypt ``t`` using the permutation ``s``."""
    s = array.array("B", s)
    t = bytearray(t)
    j = 0
    for ii in range(len(t)):
        i = (ii + 1) % 256
        j = (j + s[i]) % 256
        (s[i], s[j]) = (s[j], s[i])
        t[ii] ^= s[(s[i] + s[j]) % 256]
    return t


_icon_sys_struct = struct.Struct(
    "<4s2xH4x" "L" "16s16s16s16s" "16s16s16s" "16s16s16s" "16s" "68s64s64s64s512x"
)


def unpack_icon_sys(s: bytes) -> list:
    """Unpack an ``icon.sys`` file into a list.

    ===  ==================================================================
      0  magic (``b"PS2D"``)
      1  byte offset in the title where the second line starts
      2  background transparency
    3-6  background corner colours, as ``(r, g, b, a)`` tuples
    7-9  the three light source directions, as float 4-tuples
   10-12 the three light source colours, as float 4-tuples
     13  ambient light colour
     14  title, raw Shift-JIS bytes
   15-17 file names of the normal, copy and delete icons
    ===  ==================================================================
    """
    a = list(_icon_sys_struct.unpack(s))
    for i in range(3, 7):
        a[i] = struct.unpack("<4L", a[i])
    for i in range(7, 14):
        a[i] = struct.unpack("<4f", a[i])
    a[14] = zero_terminate(a[14])
    for i in range(15, 18):
        a[i] = decode_name(a[i])
    return a


def icon_sys_title(icon_sys, encoding=None):
    """Extract the two lines of the title stored in an icon.sys tuple."""
    offset = icon_sys[1]
    title = icon_sys[14]
    title2 = shift_jis_conv(title[offset:], encoding)
    title1 = shift_jis_conv(title[:offset], encoding)
    return (title1, title2)


def single_title(title) -> str:
    """Join the two lines of an icon.sys title into one tidy string."""
    return " ".join((title[0] + " " + title[1]).split())


def _read_fixed(f, n) -> bytes:
    """Read exactly ``n`` bytes from a file, or raise."""
    s = f.read(n)
    if len(s) != n:
        raise eof(f)
    return s


def _read_long_string(f) -> bytes:
    """Read a string prefixed with a 32-bit length from a file."""
    length = struct.unpack("<L", _read_fixed(f, 4))[0]
    return _read_fixed(f, length)


class ps2_save_file(object):
    """The state of a PlayStation 2 save file."""

    def __init__(self):
        self.file_ents = None
        self.file_data = None
        self.dirent = None
        self._defer_load_max = False
        self._compressed = None

    def set_directory(self, ent, defer=False):
        self._defer_load_max = defer
        self._compressed = None
        self.file_ents = [None] * ent[2]
        self.file_data = [None] * ent[2]
        self.dirent = list(ent)

    def set_file(self, i, ent, data):
        self.file_ents[i] = ent
        self.file_data[i] = data

    def get_directory(self):
        return self.dirent[:]

    def get_file(self, i):
        if self._defer_load_max:
            self.decompress()
        return (self.file_ents[i], self.file_data[i])

    def __len__(self):
        return self.dirent[2]

    def __getitem__(self, index):
        if index >= self.dirent[2]:
            raise IndexError(index)
        return self.get_file(index)

    def get_icon_sys(self):
        """Return the unpacked ``icon.sys`` of this save, if it has one."""
        for i in range(self.dirent[2]):
            (ent, data) = self.get_file(i)
            if ent[8] == "icon.sys" and len(data) >= ICON_SYS_LENGTH:
                return unpack_icon_sys(data[:ICON_SYS_LENGTH])
        return None

    def get_icon(self, name):
        """Return the raw contents of one of the save's icon files."""
        for i in range(self.dirent[2]):
            (ent, data) = self.get_file(i)
            if ent[8] == name:
                return data
        return None

    #
    # EMS (.psu)
    #

    def load_ems(self, f):
        """Load an EMS (.psu) save file."""
        cluster_size = 1024

        dirent = unpack_dirent(_read_fixed(f, PS2MC_DIRENT_LENGTH))
        dotent = unpack_dirent(_read_fixed(f, PS2MC_DIRENT_LENGTH))
        dotdotent = unpack_dirent(_read_fixed(f, PS2MC_DIRENT_LENGTH))
        if (
            not mode_is_dir(dirent[0])
            or not mode_is_dir(dotent[0])
            or not mode_is_dir(dotdotent[0])
            or dirent[2] < 2
        ):
            raise corrupt("Not an EMS (.psu) save file.", f)

        dirent[2] -= 2
        self.set_directory(dirent)

        for i in range(dirent[2]):
            ent = unpack_dirent(_read_fixed(f, PS2MC_DIRENT_LENGTH))
            if not mode_is_file(ent[0]):
                raise subdir(f)
            flen = ent[2]
            self.set_file(i, ent, _read_fixed(f, flen))
            _read_fixed(f, round_up(flen, cluster_size) - flen)

    def save_ems(self, f):
        """Write this save in the EMS (.psu) format."""
        cluster_size = 1024

        dirent = self.dirent[:]
        dirent[2] += 2
        f.write(pack_dirent(dirent))
        f.write(
            pack_dirent(
                (DF_RWX | DF_DIR | DF_0400 | DF_EXISTS, 0, 0, dirent[3],
                 0, 0, dirent[3], 0, ".")
            )
        )
        f.write(
            pack_dirent(
                (DF_RWX | DF_DIR | DF_0400 | DF_EXISTS, 0, 0, dirent[3],
                 0, 0, dirent[3], 0, "..")
            )
        )

        for i in range(dirent[2] - 2):
            (ent, data) = self.get_file(i)
            f.write(pack_dirent(ent))
            if not mode_is_file(ent[0]):
                raise error("Directory has a subdirectory.")
            f.write(data)
            f.write(b"\0" * (round_up(len(data), cluster_size) - len(data)))
        f.flush()

    #
    # MAX Drive (.max)
    #

    def decompress(self, progress=None):
        """Decompress a deferred MAX Drive save.

        Called automatically the first time the contents are accessed;
        call it directly to supply your own progress callback.  Does
        nothing if the save is already decompressed.
        """
        if not self._defer_load_max:
            return
        self._defer_load_max = False
        self._load_max_drive_2(progress)

    def _load_max_drive_2(self, progress=None):
        if self._compressed is None:
            return
        (length, s) = self._compressed
        self._compressed = None

        if progress is None:
            progress = lzari.stderr_progress("decompressing " + self.dirent[8] + ": ")
        s = lzari.decode(s, length, progress)
        dirlen = self.dirent[2]
        timestamp = self.dirent[3]
        off = 0
        for i in range(dirlen):
            if len(s) - off < 36:
                raise eof()
            (l, name) = struct.unpack("<L32s", s[off : off + 36])
            name = decode_name(name)
            off += 36
            data = s[off : off + l]
            if len(data) != l:
                raise eof()
            self.set_file(
                i,
                (DF_RWX | DF_FILE | DF_0400 | DF_EXISTS, 0, l, timestamp,
                 0, 0, timestamp, 0, name),
                data,
            )
            off += l
            off = round_up(off + 8, 16) - 8

    def load_max_drive(self, f, timestamp=None):
        """Load a MAX Drive (.max) save file.

        Decompression is deferred until the contents are first needed.
        """
        s = f.read(0x5C)
        magic = None
        if len(s) == 0x5C:
            (magic, crc, dirname, iconsysname, clen, dirlen, length) = struct.unpack(
                "<12sL32s32sLLL", s
            )
        if magic != PS2SAVE_MAX_MAGIC:
            raise corrupt("Not a MAX Drive save file", f)
        if clen == length:
            # some saves have the uncompressed size here
            # instead of the compressed size
            s = f.read()
        else:
            s = _read_fixed(f, clen - 4)
        dirname = decode_name(dirname)
        if timestamp is None:
            timestamp = tod_now()
        self.set_directory(
            (DF_RWX | DF_DIR | DF_0400 | DF_EXISTS, 0, dirlen, timestamp,
             0, 0, timestamp, 0, dirname),
            True,
        )
        self._compressed = (length, s)

    def save_max_drive(self, f, progress=None):
        """Write this save in the MAX Drive (.max) format."""
        iconsysname = ""
        icon_sys = self.get_icon_sys()
        if icon_sys is not None:
            title = icon_sys_title(icon_sys, "ascii")
            if len(title[0]) > 0 and title[0][-1] != " ":
                iconsysname = title[0] + " " + title[1].strip()
            else:
                iconsysname = title[0] + title[1].rstrip()

        out = bytearray()
        dirent = self.dirent
        for i in range(dirent[2]):
            (ent, data) = self.get_file(i)
            if not mode_is_file(ent[0]):
                raise error("Non-file in save file.")
            out += struct.pack("<L32s", ent[2], encode_name(ent[8])[:32])
            out += data
            out += b"\0" * (round_up(len(out) + 8, 16) - 8 - len(out))
        s = bytes(out)
        length = len(s)

        if progress is None:
            progress = lzari.stderr_progress("compressing " + dirent[8] + ": ")
        compressed = lzari.encode(s, progress)

        dirname = encode_name(dirent[8])[:32]
        icon_name = iconsysname.encode("ascii", "replace")[:32]
        hdr = struct.pack(
            "<12sL32s32sLLL", PS2SAVE_MAX_MAGIC, 0, dirname, icon_name,
            len(compressed) + 4, dirent[2], length,
        )
        crc = binascii.crc32(hdr)
        crc = binascii.crc32(compressed, crc)
        f.write(
            struct.pack(
                "<12sL32s32sLLL", PS2SAVE_MAX_MAGIC, crc & 0xFFFFFFFF, dirname,
                icon_name, len(compressed) + 4, dirent[2], length,
            )
        )
        f.write(compressed)
        f.flush()

    #
    # Code Breaker (.cbs)
    #

    def load_codebreaker(self, f):
        """Load a Code Breaker (.cbs) save file."""
        magic = f.read(4)
        if magic != PS2SAVE_CBS_MAGIC:
            raise corrupt("Not a Codebreaker save file.", f)
        (d04, hlen) = struct.unpack("<LL", _read_fixed(f, 8))
        if hlen < 92 + 32:
            raise corrupt("Header length too short.", f)
        (
            dlen, flen, dirname, created, modified, d44, d48, dirmode,
            d50, d54, d58, title,
        ) = struct.unpack("<LL32s8s8sLLLLLL%ds" % (hlen - 92), _read_fixed(f, hlen - 12))
        dirname = decode_name(dirname)
        created = unpack_tod(created)
        modified = unpack_tod(modified)

        # These fields don't always seem to be set correctly.
        if not mode_is_dir(dirmode):
            dirmode = DF_RWX | DF_DIR | DF_0400
        if tod_to_time(created) == 0:
            created = tod_now()
        if tod_to_time(modified) == 0:
            modified = tod_now()

        # flen can either be the total length of the file,
        # or the length of compressed body of the file
        body = f.read(flen)
        clen = len(body)
        if clen != flen and clen != flen - hlen:
            raise eof(f)
        body = bytes(rc4_crypt(PS2SAVE_CBS_RC4S, body))
        dcobj = zlib.decompressobj()
        body = dcobj.decompress(body, dlen)

        files = []
        while body:
            if len(body) < 64:
                raise eof(f)
            header = struct.unpack("<8s8sLHHLL32s", body[:64])
            size = header[2]
            data = body[64 : 64 + size]
            if len(data) != size:
                raise eof(f)
            body = body[64 + size :]
            files.append((header, data))

        self.set_directory(
            (dirmode, 0, len(files), created, 0, 0, modified, 0, dirname)
        )
        for i, (header, data) in enumerate(files):
            (created, modified, size, mode, h06, h08, h0C, name) = header
            name = decode_name(name)
            created = unpack_tod(created)
            modified = unpack_tod(modified)
            if not mode_is_file(mode):
                raise subdir(f)
            if tod_to_time(created) == 0:
                created = tod_now()
            if tod_to_time(modified) == 0:
                modified = tod_now()
            self.set_file(
                i, (mode, 0, size, created, 0, 0, modified, 0, name), data
            )

    #
    # SharkPort / X-Port (.sps, .xps)
    #

    def load_sharkport(self, f):
        """Load a SharkPort/X-Port (.sps, .xps) save file."""
        magic = f.read(17)
        if magic != PS2SAVE_SPS_MAGIC:
            raise corrupt("Not a SharkPort/X-Port save file.", f)
        (savetype,) = struct.unpack("<L", _read_fixed(f, 4))
        dirname = _read_long_string(f)
        datestamp = _read_long_string(f)
        comment = _read_long_string(f)

        (flen,) = struct.unpack("<L", _read_fixed(f, 4))

        (hlen, dirname, dirlen, dirmode, created, modified) = struct.unpack(
            "<H64sL8xH2x8s8s", _read_fixed(f, 98)
        )
        _read_fixed(f, hlen - 98)

        dirname = decode_name(dirname)
        created = unpack_tod(created)
        modified = unpack_tod(modified)

        # mode values are byte swapped
        dirmode = dirmode // 256 % 256 + dirmode % 256 * 256
        dirlen -= 2
        if not mode_is_dir(dirmode) or dirlen < 0:
            raise corrupt("Bad values in directory entry.", f)
        self.set_directory(
            (dirmode, 0, dirlen, created, 0, 0, modified, 0, dirname)
        )

        for i in range(dirlen):
            (hlen, name, flen, mode, created, modified) = struct.unpack(
                "<H64sL8xH2x8s8s", _read_fixed(f, 98)
            )
            if hlen < 98:
                raise corrupt("Header length too short.", f)
            _read_fixed(f, hlen - 98)
            name = decode_name(name)
            created = unpack_tod(created)
            modified = unpack_tod(modified)
            mode = mode // 256 % 256 + mode % 256 * 256
            if not mode_is_file(mode):
                raise subdir(f)
            self.set_file(
                i,
                (mode, 0, flen, created, 0, 0, modified, 0, name),
                _read_fixed(f, flen),
            )

        # ignore 4 byte checksum at the end


def detect_file_type(f) -> str:
    """Detect the type of a PS2 save file.

    The file-like object ``f`` should be positioned at the start of the
    file.  Returns ``"max"``, ``"psu"``, ``"cbs"``, ``"sps"``, ``"npo"``
    or ``None``.
    """
    hdr = f.read(PS2MC_DIRENT_LENGTH * 3)
    if hdr[:12] == PS2SAVE_MAX_MAGIC:
        return "max"
    if hdr[:17] == PS2SAVE_SPS_MAGIC:
        return "sps"
    if hdr[:4] == PS2SAVE_CBS_MAGIC:
        return "cbs"
    if hdr[:5] == PS2SAVE_NPO_MAGIC:
        return "npo"
    #
    # EMS (.psu) save files don't have a magic number.  Check to
    # see if it looks enough like one.
    #
    if len(hdr) != PS2MC_DIRENT_LENGTH * 3:
        return None
    dirent = unpack_dirent(hdr[:PS2MC_DIRENT_LENGTH])
    dotent = unpack_dirent(hdr[PS2MC_DIRENT_LENGTH : PS2MC_DIRENT_LENGTH * 2])
    dotdotent = unpack_dirent(hdr[PS2MC_DIRENT_LENGTH * 2 :])
    if (
        mode_is_dir(dirent[0])
        and mode_is_dir(dotent[0])
        and mode_is_dir(dotdotent[0])
        and dirent[2] >= 2
        and dotent[8] == "."
        and dotdotent[8] == ".."
    ):
        return "psu"
    return None


#: Save file types that can be read, mapped to the loader method name.
_LOADERS = {
    "max": "load_max_drive",
    "psu": "load_ems",
    "cbs": "load_codebreaker",
    "sps": "load_sharkport",
}


def load_save_file(f) -> ps2_save_file:
    """Detect the format of an open save file and load it.

    Raises :class:`error` if the format is unsupported or unrecognised.
    """
    ftype = detect_file_type(f)
    f.seek(0)
    if ftype == "npo":
        raise error("nPort saves are not supported.")
    loader = _LOADERS.get(ftype)
    if loader is None:
        raise error("Save file format not recognized.")
    sf = ps2_save_file()
    getattr(sf, loader)(f)
    return sf


#
# Set up tables of illegal and problematic characters in file names.
#
_bad_filename_chars = "".join(map(chr, range(32))) + "".join(
    map(chr, range(127, 256))
)
_bad_filename_repl = "_" * len(_bad_filename_chars)

if os.name in ("nt", "os2", "ce"):
    _bad_filename_chars += '<>:"/\\|?*'
    _bad_filename_repl += "()_'_____"
    _bad_filename_chars2 = _bad_filename_chars + " "
    _bad_filename_repl2 = _bad_filename_repl + "_"
else:
    # macOS and other POSIX systems: "/" is the only truly illegal
    # character, but a handful of others make life at a shell prompt
    # unpleasant enough to be worth replacing in "long" names.
    _bad_filename_chars += "/"
    _bad_filename_repl += "_"
    _bad_filename_chars2 = _bad_filename_chars + "?*'&|:[<>] \\\""
    _bad_filename_repl2 = _bad_filename_repl + "______(())___"

_filename_trans = str.maketrans(_bad_filename_chars, _bad_filename_repl)
_filename_trans2 = str.maketrans(_bad_filename_chars2, _bad_filename_repl2)


def fix_filename(filename: str) -> str:
    """Replace illegal or problematic characters in a filename."""
    return filename.translate(_filename_trans)


def make_longname(dirname: str, sf: ps2_save_file) -> str:
    """Return a verbose, human readable filename for a save file."""
    icon_sys = sf.get_icon_sys()
    title = ""
    if icon_sys is not None:
        title = single_title(icon_sys_title(icon_sys, "ascii"))
    crc = binascii.crc32(b"")
    for (ent, data) in sf:
        crc = binascii.crc32(data, crc)
    if len(dirname) >= 12 and dirname[0:2] in ("BA", "BJ", "BE", "BK"):
        if dirname[2:6] == "DATA":
            title = ""
        else:
            dirname = dirname[2:12]

    return fix_filename("%s %s (%08X)" % (dirname, title, crc & 0xFFFFFFFF))
