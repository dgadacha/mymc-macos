"""Haruhiko Okumura's LZARI compression, as used by MAX Drive save files.

Python 3 port of ``lzari.py`` by Ross Ridge.  Largely based on LZARI.C;
one key difference is the use of a two level dictionary look-up during
compression rather than LZARI.C's binary search tree.

The arithmetic coder relies on truncating integer division throughout, so
every division here is a floor division -- Python 2's ``/`` on positive
integers behaved that way, Python 3's does not.
"""

import array
import sys
import time
from bisect import bisect_right
from math import log

try:
    import numpy as _np
except ImportError:  # pragma: no cover - NumPy is an optional speed-up
    _np = None

__all__ = [
    "lzari_codec",
    "encode",
    "decode",
    "string_to_bit_array",
    "bit_array_to_string",
    "stderr_progress",
]

#
# Fundamental constants of the LZARI compression algorithm.
#
# Changing any of these values will create an incompatible implementation.
#

HIST_LEN = 4096
MIN_MATCH_LEN = 3
MAX_MATCH_LEN = 60

ARITH_BITS = 15
QUADRANT1 = 1 << ARITH_BITS
QUADRANT2 = QUADRANT1 * 2
QUADRANT3 = QUADRANT1 * 3
QUADRANT4 = QUADRANT1 * 4
MAX_CUM = QUADRANT1 - 1
MAX_CHAR = 256 + MAX_MATCH_LEN - MIN_MATCH_LEN + 1

#
# Other constants specific to this implementation
#

MAX_SUFFIX_CHAIN = 50  # limit on how many identical suffixes to try to match


def stderr_progress(prefix):
    """Build a progress callback that draws a percentage on stderr.

    The percentage is redrawn in place, so it is only emitted when stderr
    is a terminal; piping or redirecting output keeps it clean.
    """
    try:
        interactive = sys.stderr.isatty()
    except (AttributeError, ValueError):
        interactive = False

    if not interactive:
        return None

    def report(percent):
        if percent >= 100:
            sys.stderr.write("%s100%%\n" % prefix)
        else:
            sys.stderr.write("%s%3d%%\r" % (prefix, percent))
        sys.stderr.flush()

    return report


_BIT_TABLE = [bytes((b >> i) & 1 for i in range(7, -1, -1)) for b in range(256)]


def string_to_bit_array(s):
    """Convert a byte string to an array of individual bits, MSB first."""
    a = array.array("B")
    if _np is not None:
        a.frombytes(_np.unpackbits(_np.frombuffer(bytes(s), dtype=_np.uint8)).tobytes())
    else:
        buf = bytearray()
        for b in s:
            buf += _BIT_TABLE[b]
        a.frombytes(bytes(buf))
    return a


def bit_array_to_string(a):
    """Convert an array of individual bits back to a byte string."""
    remainder = len(a) % 8
    if remainder != 0:
        a.fromlist([0] * (8 - remainder))
    if _np is not None:
        return _np.packbits(_np.frombuffer(a.tobytes(), dtype=_np.uint8)).tobytes()
    out = bytearray(len(a) // 8)
    for i in range(len(out)):
        byte = 0
        for bit in a[i * 8 : i * 8 + 8]:
            byte = (byte << 1) | bit
        out[i] = byte
    return bytes(out)


def _match(src, pos, hpos, mlen, end):
    mlen += 1
    if not src.startswith(src[hpos : hpos + mlen], pos):
        return None
    for i in range(mlen, end):
        if src[pos + i] != src[hpos + i]:
            return i
    return end


def _rehash_table2(src, chars, head, next_tbl, next2_tbl, hist_invalid):
    p = head
    table2 = {}
    chain = []
    while p > hist_invalid:
        chain.append(p)
        p = next_tbl[p % HIST_LEN]
    chain.reverse()
    for p in chain:
        p2 = p + MIN_MATCH_LEN
        key2 = src[p2 : p2 + chars]
        head2 = table2.get(key2, hist_invalid)
        next2_tbl[p % HIST_LEN] = head2
        table2[key2] = p
    return table2


class lzari_codec(object):
    """An LZARI encoder/decoder.

    Despite the name this does not implement a codec compatible with
    Python's codec system.
    """

    def init(self, decode):
        self.high = QUADRANT4
        self.low = 0
        if decode:
            self.code = 0
            # reverse the order of sym_cum so bisect_right() can
            # be used for faster searching
            self.sym_cum = list(range(0, MAX_CHAR + 1))
        else:
            self.shifts = 0
            self.char_to_symbol = list(range(1, MAX_CHAR + 1))
            self.sym_cum = list(range(MAX_CHAR, -1, -1))
            self.next_table = [None] * HIST_LEN
            self.next2_table = [None] * HIST_LEN
            self.suffix_table = {}

        self.symbol_to_char = [0] + list(range(MAX_CHAR))
        self.sym_freq = [0] + [1] * MAX_CHAR
        self.position_cum = [0] * (HIST_LEN + 1)
        a = 0
        for i in range(HIST_LEN, 0, -1):
            a = a + 10000 // (200 + i)
            self.position_cum[i - 1] = a

    def search(self, table, x):
        c = 1
        s = len(table) - 1
        while True:
            a = (s + c) // 2
            if table[a] <= x:
                s = a
            else:
                c = a + 1
            if c >= s:
                break
        return c

    def update_model_decode(self, symbol):
        # A compatible implementation to the one used while compressing.

        sym_freq = self.sym_freq
        sym_cum = self.sym_cum

        if sym_cum[MAX_CHAR] >= MAX_CUM:
            c = 0
            for i in range(MAX_CHAR, 0, -1):
                sym_cum[MAX_CHAR - i] = c
                a = (sym_freq[i] + 1) // 2
                sym_freq[i] = a
                c += a
            sym_cum[MAX_CHAR] = c
        freq = sym_freq[symbol]
        new_symbol = symbol
        while sym_freq[new_symbol - 1] == freq:
            new_symbol -= 1
        if new_symbol != symbol:
            symbol_to_char = self.symbol_to_char
            swap_char = symbol_to_char[new_symbol]
            char = symbol_to_char[symbol]
            symbol_to_char[new_symbol] = char
            symbol_to_char[symbol] = swap_char
        sym_freq[new_symbol] = freq + 1
        for i in range(MAX_CHAR - new_symbol + 1, MAX_CHAR + 1):
            sym_cum[i] += 1

    def update_model_encode(self, symbol):
        sym_freq = self.sym_freq
        sym_cum = self.sym_cum

        if sym_cum[0] >= MAX_CUM:
            c = 0
            for i in range(MAX_CHAR, 0, -1):
                sym_cum[i] = c
                a = (sym_freq[i] + 1) // 2
                sym_freq[i] = a
                c += a
            sym_cum[0] = c
        freq = sym_freq[symbol]
        new_symbol = symbol
        while sym_freq[new_symbol - 1] == freq:
            new_symbol -= 1
        if new_symbol != symbol:
            swap_char = self.symbol_to_char[new_symbol]
            char = self.symbol_to_char[symbol]
            self.symbol_to_char[new_symbol] = char
            self.symbol_to_char[symbol] = swap_char
            self.char_to_symbol[char] = new_symbol
            self.char_to_symbol[swap_char] = symbol
        sym_freq[new_symbol] += 1
        for i in range(new_symbol):
            sym_cum[i] += 1

    def decode_char(self):
        high = self.high
        low = self.low
        code = self.code
        sym_cum = self.sym_cum

        _range = high - low
        max_cum_freq = sym_cum[MAX_CHAR]
        n = ((code - low + 1) * max_cum_freq - 1) // _range
        i = bisect_right(sym_cum, n, 1)
        high = low + sym_cum[i] * _range // max_cum_freq
        low += sym_cum[i - 1] * _range // max_cum_freq
        symbol = MAX_CHAR + 1 - i

        while True:
            if low < QUADRANT2:
                if low < QUADRANT1 or high > QUADRANT3:
                    if high > QUADRANT2:
                        break
                else:
                    low -= QUADRANT1
                    code -= QUADRANT1
                    high -= QUADRANT1
            else:
                low -= QUADRANT2
                code -= QUADRANT2
                high -= QUADRANT2
            low *= 2
            high *= 2
            code = code * 2 + self.in_iter()

        ret = self.symbol_to_char[symbol]
        self.high = high
        self.low = low
        self.code = code
        self.update_model_decode(symbol)
        return ret

    def decode_position(self):
        _range = self.high - self.low
        max_cum = self.position_cum[0]
        pos = (
            self.search(
                self.position_cum,
                ((self.code - self.low + 1) * max_cum - 1) // _range,
            )
            - 1
        )
        self.high = self.low + self.position_cum[pos] * _range // max_cum
        self.low += self.position_cum[pos + 1] * _range // max_cum
        while True:
            if self.low < QUADRANT2:
                if self.low < QUADRANT1 or self.high > QUADRANT3:
                    if self.high > QUADRANT2:
                        return pos
                else:
                    self.low -= QUADRANT1
                    self.code -= QUADRANT1
                    self.high -= QUADRANT1
            else:
                self.low -= QUADRANT2
                self.code -= QUADRANT2
                self.high -= QUADRANT2
            self.low *= 2
            self.high *= 2
            self.code = self.in_iter() + self.code * 2

    def add_suffix_1(self, pos, find):
        # naive implementation used for testing

        if not find:
            return (None, 0)
        src = self.src
        mlen = min(1000, self.max_match, len(src) - pos)
        hist_start = max(pos - HIST_LEN, 0)
        while mlen >= MIN_MATCH_LEN:
            i = src.rfind(src[pos : pos + mlen], hist_start, pos)
            if i != -1:
                assert src[pos : pos + mlen] == src[i : i + mlen]
                return (i, mlen)
            mlen -= 1
        return (None, -1)

    def add_suffix_2(self, pos, find):
        # a two level dictionary look up that leverages Python's
        # built-in dicts to get something that's hopefully faster
        # than implementing binary trees completely in Python.

        src = self.src
        suffix_table = self.suffix_table
        max_match = min(self.max_match, len(src) - pos)

        mlen = -1
        mpos = None

        hist_invalid = pos - HIST_LEN - 1
        modpos = pos % HIST_LEN
        pos2 = pos + MIN_MATCH_LEN

        key = src[pos:pos2]
        a = suffix_table.get(key)
        if a is not None:
            next_tbl = self.next_table
            next2_tbl = self.next2_table

            [count, head, table2, chars] = a

            pos3 = pos2 + chars
            key2 = src[pos2:pos3]
            min_match2 = MIN_MATCH_LEN + chars
            if find:
                p = table2.get(key2, hist_invalid)
                maxmlen = max_match - min_match2
                while p > hist_invalid and mlen != maxmlen:
                    p3 = p + min_match2
                    if mpos is None and p3 <= pos:
                        mpos = p
                        mlen = 0
                    if p3 >= pos:
                        p = next2_tbl[p % HIST_LEN]
                        continue
                    rlen = _match(src, pos3, p3, mlen, min(maxmlen, pos - p3))
                    if rlen is not None:
                        mpos = p
                        mlen = rlen
                    p = next2_tbl[p % HIST_LEN]
            if mpos is not None:
                mlen += min_match2
            elif find:
                p = head
                maxmlen = min(chars, max_match - MIN_MATCH_LEN)
                i = 0
                while p > hist_invalid and i < 50000 and mlen < maxmlen:
                    assert i < count
                    i += 1
                    p2 = p + MIN_MATCH_LEN
                    l2 = pos - p2
                    if mpos is None and l2 >= 0:
                        mpos = p
                        mlen = 0
                    if l2 <= 0:
                        p = next_tbl[p % HIST_LEN]
                        continue
                    if l2 > maxmlen:
                        l2 = maxmlen
                    m = mlen + 1
                    if src.startswith(src[p2 : p2 + m], pos2):
                        mpos = p
                        for j in range(m, l2):
                            if src[pos2 + j] != src[p2 + j]:
                                mlen = j
                                break
                        else:
                            mlen = l2
                    p = next_tbl[p % HIST_LEN]

                if mpos is not None:
                    mlen += MIN_MATCH_LEN

            count += 1
            new_chars = int(log(count, 2))
            new_chars = min(new_chars, max_match - MIN_MATCH_LEN)
            if new_chars > chars:
                chars = new_chars
                table2 = _rehash_table2(
                    src, chars, head, next_tbl, next2_tbl, hist_invalid
                )

            next_tbl[modpos] = head
            head = pos

            key2 = src[pos2 : pos2 + chars]
            head2 = table2.get(key2, hist_invalid)
            next2_tbl[modpos] = head2
            table2[key2] = pos

            a[0] = count
            a[1] = head
            a[2] = table2
            a[3] = chars
        else:
            self.next_table[modpos] = hist_invalid
            self.next2_table[modpos] = hist_invalid
            key2 = b""
            suffix_table[key] = [1, pos, {key2: pos}, len(key2)]

        p = pos - HIST_LEN
        if p >= 0:
            p2 = p + MIN_MATCH_LEN
            key = src[p:p2]
            a = suffix_table[key]
            (count, head, table2, chars) = a
            count -= 1
            if count == 0:
                assert head == p
                del suffix_table[key]
            else:
                key2 = src[p2 : p2 + chars]
                if table2[key2] == p:
                    del table2[key2]
                a[0] = count
        assert mpos is None or src[pos : pos + mlen] == src[mpos : mpos + mlen]
        return (mpos, mlen)

    add_suffix = add_suffix_2

    def output_bit(self, bit):
        self.append_bit(bit)
        bit ^= 1
        for _ in range(self.shifts):
            self.append_bit(bit)
        self.shifts = 0

    def encode_char(self, char):
        low = self.low
        high = self.high
        sym_cum = self.sym_cum

        symbol = self.char_to_symbol[char]
        rng = high - low

        high = low + rng * sym_cum[symbol - 1] // sym_cum[0]
        low += rng * sym_cum[symbol] // sym_cum[0]
        while True:
            if high <= QUADRANT2:
                self.output_bit(0)
            elif low >= QUADRANT2:
                self.output_bit(1)
                low -= QUADRANT2
                high -= QUADRANT2
            elif low >= QUADRANT1 and high <= QUADRANT3:
                self.shifts += 1
                low -= QUADRANT1
                high -= QUADRANT1
            else:
                break
            low *= 2
            high *= 2
        self.low = low
        self.high = high
        self.update_model_encode(symbol)

    def encode_position(self, position):
        position_cum = self.position_cum
        low = self.low
        high = self.high

        rng = high - low
        high = low + rng * position_cum[position] // position_cum[0]
        low += rng * position_cum[position + 1] // position_cum[0]

        while True:
            if high <= QUADRANT2:
                self.output_bit(0)
            elif low >= QUADRANT2:
                self.output_bit(1)
                low -= QUADRANT2
                high -= QUADRANT2
            elif low >= QUADRANT1 and high <= QUADRANT3:
                self.shifts += 1
                low -= QUADRANT1
                high -= QUADRANT1
            else:
                break
            low *= 2
            high *= 2

        self.low = low
        self.high = high

    def encode(self, src, progress=None):
        """Compress a byte string."""

        src = bytes(src)
        length = len(src)
        if length == 0:
            return b""

        out_array = array.array("B")
        self.out_array = out_array
        self.append_bit = out_array.append

        self.init(False)

        max_match = min(MAX_MATCH_LEN, length)
        self.max_match = max_match
        self.src = src = b"\x20" * max_match + src

        in_length = len(src)

        self.start_pos = max_match

        for in_pos in range(max_match):
            self.add_suffix(in_pos, False)
        in_pos = max_match
        last_percent = -1
        while in_pos < in_length:
            if progress:
                percent = (in_pos - max_match) * 100 // length
                if percent != last_percent:
                    progress(percent)
                    last_percent = percent
            (match_pos, match_len) = self.add_suffix(in_pos, True)
            if match_len < MIN_MATCH_LEN:
                self.encode_char(src[in_pos])
            else:
                self.encode_char(256 - MIN_MATCH_LEN + match_len)
                self.encode_position(in_pos - match_pos - 1)
                for _ in range(match_len - 1):
                    in_pos += 1
                    self.add_suffix(in_pos, False)
            in_pos += 1

        self.shifts += 1
        if self.low < QUADRANT1:
            self.output_bit(0)
        else:
            self.output_bit(1)

        if progress:
            progress(100)

        return bit_array_to_string(out_array)

    def decode(self, src, out_length, progress=None):
        """Decompress a byte string."""

        a = string_to_bit_array(src)
        a.fromlist([0] * 32)  # add some extra bits
        self.in_iter = iter(a).__next__

        out = bytearray(out_length)
        outpos = 0

        self.init(True)

        self.code = 0
        for _ in range(ARITH_BITS + 2):
            self.code += self.code + self.in_iter()

        hist_pos = HIST_LEN - MAX_MATCH_LEN
        history = [0x20] * hist_pos + [0] * MAX_MATCH_LEN

        decode_char = self.decode_char
        last_percent = -1
        last_time = time.monotonic()
        while outpos < out_length:
            if progress:
                percent = outpos * 100 // out_length
                if percent != last_percent:
                    now = time.monotonic()
                    if now - last_time >= 1:
                        progress(percent)
                        last_percent = percent
                        last_time = now
            char = decode_char()
            if char >= 0x100:
                pos = self.decode_position()
                length = char - 0x100 + MIN_MATCH_LEN
                base = (hist_pos - pos - 1) % HIST_LEN
                for off in range(length):
                    b = history[(base + off) % HIST_LEN]
                    out[outpos] = b
                    outpos += 1
                    history[hist_pos] = b
                    hist_pos = (hist_pos + 1) % HIST_LEN
            else:
                out[outpos] = char
                outpos += 1
                history[hist_pos] = char
                hist_pos = (hist_pos + 1) % HIST_LEN

        self.in_iter = None
        if progress:
            progress(100)
        return bytes(out)


def decode(src, out_length, progress=None):
    """Decompress ``src`` into exactly ``out_length`` bytes."""
    return lzari_codec().decode(src, out_length, progress)


def encode(src, progress=None):
    """Compress ``src``."""
    return lzari_codec().encode(src, progress)
