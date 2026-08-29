"""Hamming codes (ECC) as used on PS2 memory cards.

Python 3 port of ``ps2mc_ecc.py`` by Ross Ridge (public domain).

Every 128 byte chunk of a page is protected by three bytes of ECC, which
is enough to correct any single bit error and to detect most larger ones.
The original relied on a Windows DLL (``mymcsup``) to make this bearably
fast; here NumPy does the same job when it is installed, and a pure Python
implementation takes over when it is not.
"""

from .rounding import div_round_up

try:
    import numpy as _np
except ImportError:  # pragma: no cover - NumPy is an optional speed-up
    _np = None

__all__ = [
    "ECC_CHECK_OK",
    "ECC_CHECK_CORRECTED",
    "ECC_CHECK_FAILED",
    "ecc_calculate",
    "ecc_check",
    "ecc_calculate_page",
    "ecc_check_page",
    "have_fast_ecc",
]

ECC_CHECK_OK = 0
ECC_CHECK_CORRECTED = 1
ECC_CHECK_FAILED = 2

CHUNK_SIZE = 128


def _popcount(a: int) -> int:
    return bin(a).count("1")


def _parityb(a: int) -> int:
    a ^= a >> 1
    a ^= a >> 2
    a ^= a >> 4
    return a & 1


def _make_ecc_tables():
    parity_table = [_parityb(b) for b in range(256)]
    cpmasks = [0x55, 0x33, 0x0F, 0x00, 0xAA, 0xCC, 0xF0]

    column_parity_masks = [0] * 256
    for b in range(256):
        mask = 0
        for i, cpmask in enumerate(cpmasks):
            mask |= parity_table[b & cpmask] << i
        column_parity_masks[b] = mask

    return parity_table, column_parity_masks


_parity_table, _column_parity_masks = _make_ecc_tables()

if _np is not None:
    _np_parity = _np.array(_parity_table, dtype=_np.uint8)
    _np_cpm = _np.array(_column_parity_masks, dtype=_np.uint8)
    _np_index = _np.arange(CHUNK_SIZE, dtype=_np.uint8)
    _np_index_inv = (~_np_index) & 0x7F

have_fast_ecc = _np is not None


def ecc_calculate(s) -> list:
    """Calculate the Hamming code of a chunk of at most 128 bytes.

    Returns ``[column_parity, line_parity_0, line_parity_1]``.
    """
    column_parity = 0x77
    line_parity_0 = 0x7F
    line_parity_1 = 0x7F
    for i, b in enumerate(s):
        column_parity ^= _column_parity_masks[b]
        if _parity_table[b]:
            line_parity_0 ^= ~i
            line_parity_1 ^= i
    return [column_parity, line_parity_0 & 0x7F, line_parity_1]


def ecc_check(s, ecc) -> int:
    """Detect and correct any single bit error in one 128 byte chunk.

    ``s`` and ``ecc`` -- the data and its expected Hamming code -- must be
    mutable sequences of integers; both are updated in place when a
    correction is made.
    """
    computed = ecc_calculate(s)
    if computed == list(ecc):
        return ECC_CHECK_OK

    cp_diff = (computed[0] ^ ecc[0]) & 0x77
    lp0_diff = (computed[1] ^ ecc[1]) & 0x7F
    lp1_diff = (computed[2] ^ ecc[2]) & 0x7F
    lp_comp = lp0_diff ^ lp1_diff
    cp_comp = (cp_diff >> 4) ^ (cp_diff & 0x07)

    if lp_comp == 0x7F and cp_comp == 0x07:
        # correctable single bit error in the data
        s[lp1_diff] ^= 1 << (cp_diff >> 4)
        return ECC_CHECK_CORRECTED
    if (cp_diff == 0 and lp0_diff == 0 and lp1_diff == 0) or _popcount(
        lp_comp
    ) + _popcount(cp_comp) == 1:
        # correctable single bit error in the ECC itself
        # (and/or one of the unused bits was set)
        ecc[0] = computed[0]
        ecc[1] = computed[1]
        ecc[2] = computed[2]
        return ECC_CHECK_CORRECTED

    return ECC_CHECK_FAILED


def _ecc_calculate_page_py(page) -> bytes:
    out = bytearray()
    for i in range(div_round_up(len(page), CHUNK_SIZE)):
        out += bytes(ecc_calculate(page[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]))
    return bytes(out)


def _ecc_calculate_page_np(page) -> bytes:
    data = _np.frombuffer(bytes(page), dtype=_np.uint8)
    nchunks = div_round_up(data.size, CHUNK_SIZE)
    padded = nchunks * CHUNK_SIZE
    if data.size != padded:
        data = _np.concatenate([data, _np.zeros(padded - data.size, _np.uint8)])
    m = data.reshape(nchunks, CHUNK_SIZE)

    cp = 0x77 ^ _np.bitwise_xor.reduce(_np_cpm[m], axis=1)
    odd = _np_parity[m].astype(bool)
    lp1 = 0x7F ^ _np.bitwise_xor.reduce(_np.where(odd, _np_index, 0), axis=1)
    lp0 = 0x7F ^ _np.bitwise_xor.reduce(_np.where(odd, _np_index_inv, 0), axis=1)

    return _np.stack([cp, lp0, lp1], axis=1).astype(_np.uint8).tobytes()


def ecc_calculate_page(page) -> bytes:
    """Return the ECC bytes for a whole memory card page (3 per 128 bytes)."""
    if _np is not None:
        return _ecc_calculate_page_np(page)
    return _ecc_calculate_page_py(page)


def ecc_check_page(page: bytes, spare: bytes):
    """Check, and where possible correct, one memory card page.

    Returns ``(status, page, spare)``; ``page`` and ``spare`` are the
    corrected versions when ``status`` is ``ECC_CHECK_CORRECTED``.
    """
    nchunks = div_round_up(len(page), CHUNK_SIZE)
    expected = ecc_calculate_page(page)

    # The overwhelmingly common case: the page is intact, so a single
    # comparison of the whole page's ECC avoids all per-chunk work.
    if spare[: nchunks * 3] == expected:
        return (ECC_CHECK_OK, page, spare)

    chunks = []
    for i in range(nchunks):
        data = bytearray(page[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE])
        chunks.append((data, list(spare[i * 3 : i * 3 + 3])))

    results = [ecc_check(data, ecc) for (data, ecc) in chunks]

    ret = ECC_CHECK_OK
    if ECC_CHECK_CORRECTED in results:
        # rebuild the page and its spare area from the corrected chunks
        page = b"".join(bytes(data) for (data, _) in chunks)
        spare = bytes(bytearray(b for (_, ecc) in chunks for b in ecc)) + spare[
            nchunks * 3 :
        ]
        ret = ECC_CHECK_CORRECTED
    if ECC_CHECK_FAILED in results:
        ret = ECC_CHECK_FAILED
    return (ret, page, spare)
