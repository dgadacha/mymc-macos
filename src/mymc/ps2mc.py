"""The file system of a PlayStation 2 memory card image.

Python 3 port of ``ps2mc.py`` by Ross Ridge (public domain).

Raw file contents are ``bytes`` throughout; path names and directory
entry names are ``str``.
"""

import array
import fnmatch
import struct
import sys
import traceback
from errno import (
    EACCES,
    EBUSY,
    EEXIST,
    EINVAL,
    EIO,
    EISDIR,
    ENOENT,
    ENOSPC,
    ENOTDIR,
    ENOTEMPTY,
)

from . import ps2save
from .ps2mc_dir import (
    DF_0400,
    DF_DIR,
    DF_EXISTS,
    DF_FILE,
    DF_HIDDEN,
    DF_RWX,
    DF_WRITE,
    DF_EXECUTE,
    PS2MC_DIRENT_LENGTH,
    mode_is_dir,
    mode_is_file,
    pack_dirent,
    tod_now,
    unpack_dirent,
)
from .ps2mc_ecc import ECC_CHECK_FAILED, ecc_calculate_page, ecc_check_page
from .rounding import div_round_up, round_down, round_up

PS2MC_MAGIC = b"Sony PS2 Memory Card Format "
PS2MC_FAT_ALLOCATED_BIT = 0x80000000
PS2MC_FAT_CHAIN_END = 0xFFFFFFFF
PS2MC_FAT_CHAIN_END_UNALLOC = 0x7FFFFFFF
PS2MC_FAT_CLUSTER_MASK = 0x7FFFFFFF
PS2MC_MAX_INDIRECT_FAT_CLUSTERS = 32
PS2MC_CLUSTER_SIZE = 1024
PS2MC_INDIRECT_FAT_OFFSET = 0x2000

PS2MC_STANDARD_PAGE_SIZE = 512
PS2MC_STANDARD_PAGES_PER_CARD = 16384
PS2MC_STANDARD_PAGES_PER_ERASE_BLOCK = 16


class error(Exception):
    """Base for all exceptions specific to this module."""


class io_error(error, OSError):
    def __init__(self, *args, **kwargs):
        OSError.__init__(self, *args, **kwargs)

    def __str__(self):
        if getattr(self, "strerror", None) is None:
            return str(self.args)
        if getattr(self, "filename", None) is not None:
            return str(self.filename) + ": " + self.strerror
        return self.strerror


class path_not_found(io_error):
    def __init__(self, filename):
        io_error.__init__(self, ENOENT, "path not found", filename)


class file_not_found(io_error):
    def __init__(self, filename):
        io_error.__init__(self, ENOENT, "file not found", filename)


class dir_not_found(io_error):
    def __init__(self, filename):
        io_error.__init__(self, ENOENT, "directory not found", filename)


class dir_index_not_found(io_error, IndexError):
    def __init__(self, filename, index):
        msg = "index (%d) past end of directory" % index
        io_error.__init__(self, ENOENT, msg, filename)


class corrupt(io_error):
    def __init__(self, msg, f=None):
        filename = None
        if f is not None:
            filename = getattr(f, "name", None)
        io_error.__init__(self, EIO, msg, filename)


class ecc_error(corrupt):
    def __init__(self, msg, filename=None):
        corrupt.__init__(self, msg, filename)


# 'I' is 32 bits wide on every platform CPython supports, but check
# rather than silently corrupting a card image if that ever changes.
_U32 = "I"
if array.array(_U32).itemsize != 4:  # pragma: no cover
    _U32 = "L"
    if array.array(_U32).itemsize != 4:
        raise ImportError("no 32-bit array type available")


if sys.byteorder == "big":

    def unpack_32bit_array(s):
        a = array.array(_U32, bytes(s))
        a.byteswap()
        return a

    def pack_32bit_array(a):
        a = a[:]
        a.byteswap()
        return a.tobytes()

else:

    def unpack_32bit_array(s):
        return array.array(_U32, bytes(s))

    def pack_32bit_array(a):
        return a.tobytes()


_superblock_struct = struct.Struct("<28s12sHHHHLLLLLL8x128s128sbbxx")


def unpack_superblock(s):
    sb = list(_superblock_struct.unpack(s))
    sb[12] = unpack_32bit_array(sb[12])
    sb[13] = unpack_32bit_array(sb[13])
    return sb


def pack_superblock(sb):
    sb = list(sb)
    sb[12] = pack_32bit_array(sb[12])
    sb[13] = pack_32bit_array(sb[13])
    return _superblock_struct.pack(*sb)


unpack_fat = unpack_32bit_array
pack_fat = pack_32bit_array


def pathname_split(pathname):
    if pathname == "":
        return (None, False, False)
    components = pathname.split("/")
    return (
        [name for name in components if name != ""],
        components[0] != "",
        components[-1] == "",
    )


class lru_cache(object):
    """A fixed size least-recently-used cache."""

    def __init__(self, length):
        self._lru_list = [[i - 1, None, None, i + 1] for i in range(length + 1)]
        self._index_map = {}

    def _move_to_front(self, i):
        lru_list = self._lru_list
        first = lru_list[0]
        i2 = first[3]
        if i != i2:
            elt = lru_list[i]
            prev = lru_list[elt[0]]
            nxt = lru_list[elt[3]]
            prev[3] = elt[3]
            nxt[0] = elt[0]
            elt[0] = 0
            elt[3] = i2
            lru_list[i2][0] = i
            first[3] = i

    def add(self, key, value):
        lru_list = self._lru_list
        index_map = self._index_map
        ret = None
        if key in index_map:
            i = index_map[key]
            elt = lru_list[i]
        else:
            i = lru_list[-1][0]
            elt = lru_list[i]
            old_key = elt[1]
            if old_key is not None:
                del index_map[old_key]
                ret = (old_key, elt[2])
            index_map[key] = i
            elt[1] = key
        elt[2] = value
        self._move_to_front(i)
        return ret

    def get(self, key, default=None):
        i = self._index_map.get(key)
        if i is None:
            return default
        ret = self._lru_list[i][2]
        self._move_to_front(i)
        return ret

    def items(self):
        return [
            (elt[1], elt[2]) for elt in self._lru_list[1:-1] if elt[2] is not None
        ]


class fat_chain(object):
    """A class for accessing a file's FAT entries as a simple sequence."""

    def __init__(self, lookup_fat, first):
        self.lookup_fat = lookup_fat
        self._first = first
        self.offset = 0
        self._prev = None
        self._cur = first

    def __getitem__(self, i):
        # not iterable
        offset = self.offset
        if i == offset:
            return self._cur
        elif i == offset - 1:
            assert self._prev is not None
            return self._prev
        if i < offset:
            if i == 0:
                return self._first
            offset = 0
            prev = None
            cur = self._first
        else:
            prev = self._prev
            cur = self._cur
        nxt = cur
        while offset != i:
            nxt = self.lookup_fat(cur)
            if nxt == PS2MC_FAT_CHAIN_END:
                break
            if nxt & PS2MC_FAT_ALLOCATED_BIT:
                nxt &= ~PS2MC_FAT_ALLOCATED_BIT
            else:
                # corrupt
                nxt = PS2MC_FAT_CHAIN_END
                break

            offset += 1
            prev = cur
            cur = nxt
        self.offset = offset
        self._prev = prev
        self._cur = cur
        return nxt

    def __len__(self):
        old_prev = self._prev
        old_cur = self._cur
        old_offset = self.offset
        i = self.offset
        while self[i] != PS2MC_FAT_CHAIN_END:
            i += 1
        self._prev = old_prev
        self._cur = old_cur
        self.offset = old_offset
        return i


class ps2mc_file(object):
    """A file-like object for accessing a file in a memory card image."""

    def __init__(self, mc, dirloc, first_cluster, length, mode, name=None):
        self.mc = mc
        self.length = length
        self.first_cluster = first_cluster
        self.dirloc = dirloc
        self.fat_chain = None
        self._pos = 0
        self.buffer = None
        self.buffer_cluster = None
        self.name = "<ps2mc_file>" if name is None else name
        self.closed = False

        if not mode:
            mode = "rb"
        self.mode = mode
        self._append = False
        self._write = False
        if mode[0] == "a":
            self._append = True
        elif mode[0] != "w" or ("+" not in self.mode):
            self._write = True

    def _find_file_cluster(self, n):
        if self.fat_chain is None:
            self.fat_chain = self.mc.fat_chain(self.first_cluster)
        return self.fat_chain[n]

    def read_file_cluster(self, n):
        if n == self.buffer_cluster:
            return self.buffer
        cluster = self._find_file_cluster(n)
        if cluster == PS2MC_FAT_CHAIN_END:
            return None
        self.buffer = self.mc.read_allocatable_cluster(cluster)
        self.buffer_cluster = n
        return self.buffer

    def _extend_file(self, n):
        mc = self.mc
        cluster = mc.allocate_cluster()
        if cluster is None:
            return None
        if n == 0:
            self.first_cluster = cluster
            self.fat_chain = None
            mc.update_dirent(self.dirloc, self, cluster, None, False)
        else:
            prev = self.fat_chain[n - 1]
            mc.set_fat(prev, cluster | PS2MC_FAT_ALLOCATED_BIT)
        return cluster

    def write_file_cluster(self, n, buf):
        mc = self.mc
        cluster = self._find_file_cluster(n)
        if cluster != PS2MC_FAT_CHAIN_END:
            mc.write_allocatable_cluster(cluster, buf)
            self.buffer = buf
            self.buffer_cluster = n
            return True

        cluster_size = mc.cluster_size
        file_cluster_end = div_round_up(self.length, cluster_size)

        if len(self.fat_chain) != file_cluster_end:
            raise corrupt(
                "file length doesn't match cluster chain length", mc.f
            )

        for i in range(file_cluster_end, n):
            cluster = self._extend_file(i)
            if cluster is None:
                if i != file_cluster_end:
                    self.length = (i - 1) * cluster_size
                    mc.update_dirent(self.dirloc, self, None, self.length, True)
                return False
            mc.write_allocatable_cluster(cluster, b"\0" * cluster_size)

        cluster = self._extend_file(n)
        if cluster is None:
            return False

        mc.write_allocatable_cluster(cluster, buf)
        self.buffer = buf
        self.buffer_cluster = n
        return True

    def update_notify(self, first_cluster, length):
        if self.first_cluster != first_cluster:
            self.first_cluster = first_cluster
            self.fat_chain = None
        self.length = length
        self.buffer = None
        self.buffer_cluster = None

    def read(self, size=None, eol=None):
        if self.closed:
            raise ValueError("file is closed")

        pos = self._pos
        cluster_size = self.mc.cluster_size
        if size is None or size < 0:
            size = self.length
        size = max(min(self.length - pos, size), 0)
        ret = b""
        while size > 0:
            off = pos % cluster_size
            l = min(cluster_size - off, size)
            buf = self.read_file_cluster(pos // cluster_size)
            if buf is None:
                break
            if eol is not None:
                i = buf.find(eol, off, off + l)
                if i != -1:
                    l = i - off + 1
                    size = l
            pos += l
            self._pos = pos
            ret += buf[off : off + l]
            size -= l
        return ret

    def write(self, out, _set_modified=True):
        if self.closed:
            raise ValueError("file is closed")

        cluster_size = self.mc.cluster_size
        pos = self._pos
        if self._append:
            pos = self.length
        elif not self._write:
            raise io_error(EACCES, "file not opened for writing", self.name)

        out = bytes(out)
        size = len(out)
        i = 0
        while size > 0:
            cluster = pos // cluster_size
            off = pos % cluster_size
            l = min(cluster_size - off, size)
            s = out[i : i + l]
            pos += l
            if l == cluster_size:
                buf = s
            else:
                buf = self.read_file_cluster(cluster)
                if buf is None:
                    buf = b"\0" * cluster_size
                buf = buf[:off] + s + buf[off + l :]
            if not self.write_file_cluster(cluster, buf):
                raise io_error(ENOSPC, "out of space on image", self.name)
            self._pos = pos
            new_length = None
            if pos > self.length:
                new_length = self.length = pos
            self.mc.update_dirent(self.dirloc, self, None, new_length, _set_modified)

            i += l
            size -= l

    def close(self):
        if self.mc is not None:
            self.mc.notify_closed(self.dirloc, self)
            self.mc = None
        self.fat_chain = None
        self.buffer = None
        self.closed = True

    def __iter__(self):
        return self

    def __next__(self):
        r = self.readline()
        if r == b"":
            raise StopIteration
        return r

    def readline(self, size=None):
        return self.read(size, b"\n")

    def readlines(self, sizehint=None):
        return list(self)

    def seek(self, offset, whence=0):
        if self.closed:
            raise ValueError("file is closed")

        if whence == 1:
            base = self._pos
        elif whence == 2:
            base = self.length
        else:
            base = 0
        self._pos = max(base + offset, 0)
        return self._pos

    def tell(self):
        if self.closed:
            raise ValueError("file is closed")
        return self._pos

    def __enter__(self):
        return self

    def __exit__(self, a, b, c):
        self.close()
        return False


class ps2mc_directory(object):
    """A sequence and iterator object for directories."""

    def __init__(self, mc, dirloc, first_cluster, length, mode="rb", name=None):
        self.f = ps2mc_file(
            mc, dirloc, first_cluster, length * PS2MC_DIRENT_LENGTH, mode, name
        )
        self._iter_end = 0

    def __iter__(self):
        start = self.tell()
        if start != 0:
            start -= 1
            self.seek(start)
        self._iter_end = start
        return self

    def write_raw_ent(self, index, ent, set_modified):
        self.seek(index)
        self.f.write(pack_dirent(ent), _set_modified=set_modified)

    def __next__(self):
        dirent = self.f.read(PS2MC_DIRENT_LENGTH)
        if dirent == b"":
            if self._iter_end == 0:
                raise StopIteration
            self.seek(0)
            dirent = self.f.read(PS2MC_DIRENT_LENGTH)
        elif self.tell() == self._iter_end:
            raise StopIteration
        return unpack_dirent(dirent)

    def seek(self, offset, whence=0):
        self.f.seek(offset * PS2MC_DIRENT_LENGTH, whence)

    def tell(self):
        return self.f.tell() // PS2MC_DIRENT_LENGTH

    def __len__(self):
        return self.f.length // PS2MC_DIRENT_LENGTH

    def __getitem__(self, index):
        self.seek(index)
        dirent = self.f.read(PS2MC_DIRENT_LENGTH)
        if len(dirent) != PS2MC_DIRENT_LENGTH:
            raise dir_index_not_found(self.f.name, index)
        return unpack_dirent(dirent)

    def __setitem__(self, index, new_ent):
        ent = self[index]
        mode = ent[0]
        if (mode & DF_EXISTS) == 0:
            return
        if new_ent[0] is not None:
            mode = (new_ent[0] & ~(DF_FILE | DF_DIR | DF_EXISTS)) | (
                mode & (DF_FILE | DF_DIR | DF_EXISTS)
            )
            ent[0] = mode
        for i in (1, 3, 6, 7, 8):  # ???, created, modified, attr, name
            if new_ent[i] is not None:
                ent[i] = new_ent[i]
        self.write_raw_ent(index, ent, False)

    def close(self):
        if self.f is not None:
            self.f.close()
            self.f = None

    def __enter__(self):
        return self

    def __exit__(self, a, b, c):
        self.close()
        return False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class _root_directory(ps2mc_directory):
    """Wrapper for the cached root directory object.

    The close() method is disabled so the cached object can be reused.
    """

    def __init__(self, mc, dirloc, first_cluster, length, mode="r+b", name="/"):
        ps2mc_directory.__init__(self, mc, dirloc, first_cluster, length, mode, name)

    def close(self):
        pass

    def real_close(self):
        ps2mc_directory.close(self)

    def __del__(self):
        pass


class ps2mc(object):
    """A PlayStation 2 memory card filesystem implementation.

    The close() method must be called when the object is no longer needed,
    otherwise cycles that can't be collected by the garbage collector will
    remain.  It can also be used as a context manager.
    """

    open_files = None
    fat_cache = None

    def _calculate_derived(self):
        self.spare_size = div_round_up(self.page_size, 128) * 4
        self.raw_page_size = self.page_size + self.spare_size
        self.cluster_size = self.page_size * self.pages_per_cluster
        self.entries_per_cluster = self.page_size * self.pages_per_cluster // 4

        limit = (
            min(self.good_block2, self.good_block1)
            * self.pages_per_erase_block
            // self.pages_per_cluster
            - self.allocatable_cluster_offset
        )
        self.allocatable_cluster_limit = limit

    def __init__(self, f, ignore_ecc=False, params=None):
        self.open_files = {}
        self.fat_cache = lru_cache(12)
        self.alloc_cluster_cache = lru_cache(64)
        self.modified = False
        self.f = None
        self.rootdir = None

        f.seek(0)
        s = f.read(0x154)
        if len(s) != 0x154 or not s.startswith(PS2MC_MAGIC):
            if params is None:
                raise corrupt("Not a PS2 memory card image", f)
            self.f = f
            self.format(params)
        else:
            sb = unpack_superblock(s)
            self.version = sb[1]
            self.page_size = sb[2]
            self.pages_per_cluster = sb[3]
            self.pages_per_erase_block = sb[4]
            self.clusters_per_card = sb[6]
            self.allocatable_cluster_offset = sb[7]
            self.allocatable_cluster_end = sb[8]
            self.rootdir_fat_cluster = sb[9]
            self.good_block1 = sb[10]
            self.good_block2 = sb[11]
            self.indirect_fat_cluster_list = sb[12]
            self.bad_erase_block_list = sb[13]

            self._calculate_derived()

            self.f = f
            self.ignore_ecc = False

            try:
                self.read_page(0)
                self.ignore_ecc = ignore_ecc
            except ecc_error:
                # the error might be due to the fact the file
                # image doesn't contain ECC data
                self.spare_size = 0
                self.raw_page_size = self.page_size
                self.ignore_ecc = True

        # sanity check
        root = self._directory(None, 0, 1)
        dot = root[0]
        dotdot = root[1]
        root.close()
        if (
            dot[8] != "."
            or dotdot[8] != ".."
            or not mode_is_dir(dot[0])
            or not mode_is_dir(dotdot[0])
        ):
            raise corrupt("Root directory damaged.", self.f)

        self.fat_cursor = 0
        self.curdir = (0, 0)

    def __enter__(self):
        return self

    def __exit__(self, a, b, c):
        self.close()
        return False

    def write_superblock(self):
        s = pack_superblock(
            (
                PS2MC_MAGIC,
                self.version,
                self.page_size,
                self.pages_per_cluster,
                self.pages_per_erase_block,
                0xFF00,
                self.clusters_per_card,
                self.allocatable_cluster_offset,
                self.allocatable_cluster_end,
                self.rootdir_fat_cluster,
                self.good_block1,
                self.good_block2,
                self.indirect_fat_cluster_list,
                self.bad_erase_block_list,
                2,
                0x2B,
            )
        )
        s += b"\x00" * (self.page_size - len(s))
        self.write_page(0, s)

        page = b"\xFF" * self.raw_page_size
        self.f.seek(
            self.good_block2 * self.pages_per_erase_block * self.raw_page_size
        )
        self.f.write(page * self.pages_per_erase_block)

        self.modified = False

    def format(self, params):
        """Create (format) a new memory card image."""

        (with_ecc, page_size, pages_per_erase_block, param_pages_per_card) = params

        if pages_per_erase_block < 1:
            raise error("invalid pages per erase block (%d)" % pages_per_erase_block)

        pages_per_card = round_down(param_pages_per_card, pages_per_erase_block)
        cluster_size = PS2MC_CLUSTER_SIZE
        pages_per_cluster = cluster_size // page_size
        clusters_per_erase_block = pages_per_erase_block // pages_per_cluster
        erase_blocks_per_card = pages_per_card // pages_per_erase_block
        clusters_per_card = pages_per_card // pages_per_cluster
        epc = cluster_size // 4

        if (
            page_size < PS2MC_DIRENT_LENGTH
            or pages_per_cluster < 1
            or pages_per_cluster * page_size != cluster_size
        ):
            raise error("invalid page size (%d)" % page_size)

        good_block1 = erase_blocks_per_card - 1
        good_block2 = erase_blocks_per_card - 2
        first_ifc = div_round_up(PS2MC_INDIRECT_FAT_OFFSET, cluster_size)

        allocatable_clusters = clusters_per_card - (first_ifc + 2)
        fat_clusters = div_round_up(allocatable_clusters, epc)
        indirect_fat_clusters = div_round_up(fat_clusters, epc)
        if indirect_fat_clusters > PS2MC_MAX_INDIRECT_FAT_CLUSTERS:
            indirect_fat_clusters = PS2MC_MAX_INDIRECT_FAT_CLUSTERS
            fat_clusters = indirect_fat_clusters * epc
        allocatable_clusters = fat_clusters * epc

        allocatable_cluster_offset = first_ifc + indirect_fat_clusters + fat_clusters
        allocatable_cluster_end = (
            good_block2 * clusters_per_erase_block - allocatable_cluster_offset
        )
        if allocatable_cluster_end < 1:
            raise error("memory card image too small to be formatted")

        ifc_list = unpack_fat(b"\0\0\0\0" * PS2MC_MAX_INDIRECT_FAT_CLUSTERS)
        for i in range(indirect_fat_clusters):
            ifc_list[i] = first_ifc + i

        self.version = b"1.2.0.0"
        self.page_size = page_size
        self.pages_per_cluster = pages_per_cluster
        self.pages_per_erase_block = pages_per_erase_block
        self.clusters_per_card = clusters_per_card
        self.allocatable_cluster_offset = allocatable_cluster_offset
        self.allocatable_cluster_end = allocatable_clusters
        self.rootdir_fat_cluster = 0
        self.good_block1 = good_block1
        self.good_block2 = good_block2
        self.indirect_fat_cluster_list = ifc_list
        self.bad_erase_block_list = unpack_32bit_array(b"\xFF\xFF\xFF\xFF" * 32)

        self._calculate_derived()

        self.ignore_ecc = not with_ecc
        erased = b"\0" * page_size
        if not with_ecc:
            self.spare_size = 0
            self.raw_page_size = page_size
        else:
            ecc = ecc_calculate_page(erased)
            erased += ecc + b"\0" * (self.spare_size - len(ecc))

        # Write the blank card in large chunks rather than a page at a
        # time; formatting a standard 8 MB image is 16384 pages.
        self.f.seek(0)
        pages_per_write = 512
        block = erased * pages_per_write
        full, remainder = divmod(pages_per_card, pages_per_write)
        for _ in range(full):
            self.f.write(block)
        if remainder:
            self.f.write(erased * remainder)

        self.modified = True

        first_fat_cluster = first_ifc + indirect_fat_clusters
        remainder = fat_clusters % epc
        for i in range(indirect_fat_clusters):
            base = first_fat_cluster + i * epc
            buf = array.array(_U32, range(base, base + epc))
            if i == indirect_fat_clusters - 1 and remainder != 0:
                del buf[remainder:]
                buf.fromlist([0xFFFFFFFF] * (epc - remainder))
            self._write_fat_cluster(ifc_list[i], buf)

        # go through the fat backwards for better cache usage
        for i in range(allocatable_clusters - 1, allocatable_cluster_end - 1, -1):
            self.set_fat(i, PS2MC_FAT_CHAIN_END)
        for i in range(allocatable_cluster_end - 1, 0, -1):
            self.set_fat(i, PS2MC_FAT_CLUSTER_MASK)
        self.set_fat(0, PS2MC_FAT_CHAIN_END)

        self.allocatable_cluster_end = allocatable_cluster_end

        now = tod_now()
        s = pack_dirent(
            (DF_RWX | DF_DIR | DF_0400 | DF_EXISTS, 0, 2, now, 0, 0, now, 0, ".")
        )
        s += b"\0" * (cluster_size - len(s))
        self.write_allocatable_cluster(0, s)
        d = self._directory((0, 0), 0, 2, "wb", "/")
        d.write_raw_ent(
            1,
            (
                DF_WRITE | DF_EXECUTE | DF_DIR | DF_0400 | DF_HIDDEN | DF_EXISTS,
                0, 0, now, 0, 0, now, 0, "..",
            ),
            False,
        )
        d.close()

        self.flush()

    def read_page(self, n):
        f = self.f
        f.seek(self.raw_page_size * n)
        page = f.read(self.page_size)
        if len(page) != self.page_size:
            raise corrupt("attempted to read past EOF (page %05X)" % n, f)
        if self.ignore_ecc:
            return page
        spare = f.read(self.spare_size)
        if len(spare) != self.spare_size:
            raise corrupt("attempted to read past EOF (page %05X)" % n, f)
        (status, page, spare) = ecc_check_page(page, spare)
        if status == ECC_CHECK_FAILED:
            raise ecc_error("Unrecoverable ECC error (page %d)" % n)
        return page

    def write_page(self, n, buf):
        f = self.f
        f.seek(self.raw_page_size * n)
        self.modified = True
        if len(buf) != self.page_size:
            raise error(
                "internal error: write_page: %d != %d" % (len(buf), self.page_size)
            )
        f.write(buf)
        if self.spare_size != 0:
            ecc = ecc_calculate_page(buf)
            f.write(ecc + b"\0" * (self.spare_size - len(ecc)))

    def read_cluster(self, n):
        pages_per_cluster = self.pages_per_cluster
        if self.spare_size == 0:
            self.f.seek(self.cluster_size * n)
            return self.f.read(self.cluster_size)
        n *= pages_per_cluster
        if pages_per_cluster == 2:
            return self.read_page(n) + self.read_page(n + 1)
        return b"".join(
            self.read_page(i) for i in range(n, n + pages_per_cluster)
        )

    def write_cluster(self, n, buf):
        pages_per_cluster = self.pages_per_cluster
        cluster_size = self.cluster_size
        if self.spare_size == 0:
            self.f.seek(cluster_size * n)
            if len(buf) != cluster_size:
                raise error(
                    "internal error: write_cluster: %d != %d"
                    % (len(buf), cluster_size)
                )
            return self.f.write(buf)
        n *= pages_per_cluster
        pgsize = self.page_size
        for i in range(pages_per_cluster):
            self.write_page(n + i, buf[i * pgsize : i * pgsize + pgsize])

    def _add_fat_cluster_to_cache(self, n, fat, dirty):
        old = self.fat_cache.add(n, [fat, dirty])
        if old is not None:
            (n, [fat, dirty]) = old
            if dirty:
                self.write_cluster(n, pack_fat(fat))

    def _read_fat_cluster(self, n):
        v = self.fat_cache.get(n)
        if v is not None:
            return v[0]
        fat = unpack_fat(self.read_cluster(n))
        self._add_fat_cluster_to_cache(n, fat, False)
        return fat

    def _write_fat_cluster(self, n, fat):
        self._add_fat_cluster_to_cache(n, fat, True)

    def flush_fat_cache(self):
        if self.fat_cache is None:
            return
        for (n, v) in self.fat_cache.items():
            [fat, dirty] = v
            if dirty:
                self.write_cluster(n, pack_fat(fat))
                v[1] = False

    def _add_alloc_cluster_to_cache(self, n, buf, dirty):
        old = self.alloc_cluster_cache.add(n, [buf, dirty])
        if old is not None:
            (n, [buf, dirty]) = old
            if dirty:
                n += self.allocatable_cluster_offset
                self.write_cluster(n, buf)

    def read_allocatable_cluster(self, n):
        a = self.alloc_cluster_cache.get(n)
        if a is not None:
            return a[0]
        buf = self.read_cluster(n + self.allocatable_cluster_offset)
        self._add_alloc_cluster_to_cache(n, buf, False)
        return buf

    def write_allocatable_cluster(self, n, buf):
        self._add_alloc_cluster_to_cache(n, buf, True)

    def flush_alloc_cluster_cache(self):
        if self.alloc_cluster_cache is None:
            return
        for (n, a) in self.alloc_cluster_cache.items():
            [buf, dirty] = a
            if dirty:
                self.write_cluster(n + self.allocatable_cluster_offset, buf)
                a[1] = False

    def read_fat_cluster(self, n):
        indirect_offset = n % self.entries_per_cluster
        dbl_offset = n // self.entries_per_cluster
        indirect_cluster = self.indirect_fat_cluster_list[dbl_offset]
        indirect_fat = self._read_fat_cluster(indirect_cluster)
        cluster = indirect_fat[indirect_offset]
        return (self._read_fat_cluster(cluster), cluster)

    def read_fat(self, n):
        if n < 0 or n >= self.allocatable_cluster_end:
            raise io_error(EIO, "FAT cluster index out of range (%d)" % n)
        offset = n % self.entries_per_cluster
        fat_cluster = n // self.entries_per_cluster
        (fat, cluster) = self.read_fat_cluster(fat_cluster)
        return (fat, offset, cluster)

    def lookup_fat(self, n):
        (fat, offset, cluster) = self.read_fat(n)
        return fat[offset]

    def set_fat(self, n, value):
        (fat, offset, cluster) = self.read_fat(n)
        fat[offset] = value
        self._write_fat_cluster(cluster, fat)

    def allocate_cluster(self):
        epc = self.entries_per_cluster
        allocatable_cluster_limit = self.allocatable_cluster_limit

        end = div_round_up(allocatable_cluster_limit, epc)
        remainder = allocatable_cluster_limit % epc

        while self.fat_cursor < end:
            (fat, cluster) = self.read_fat_cluster(self.fat_cursor)
            if self.fat_cursor == end - 1 and remainder != 0:
                n = min(fat[:remainder])
            else:
                n = min(fat)
            if (n & PS2MC_FAT_ALLOCATED_BIT) == 0:
                offset = fat.index(n)
                fat[offset] = PS2MC_FAT_CHAIN_END
                self._write_fat_cluster(cluster, fat)
                return self.fat_cursor * epc + offset
            self.fat_cursor += 1
        return None

    def fat_chain(self, first_cluster):
        return fat_chain(self.lookup_fat, first_cluster)

    def file(self, dirloc, first_cluster, length, mode, name=None):
        """Create a new file-like object for a file."""
        f = ps2mc_file(self, dirloc, first_cluster, length, mode, name)
        if dirloc is None:
            return f
        open_files = self.open_files
        if dirloc not in open_files:
            open_files[dirloc] = [None, {f}]
        else:
            open_files[dirloc][1].add(f)
        return f

    def directory(self, dirloc, first_cluster, length, mode=None, name=None):
        return ps2mc_directory(self, dirloc, first_cluster, length, mode, name)

    def _directory(self, dirloc, first_cluster, length, mode=None, name=None):
        if first_cluster != 0:
            return self.directory(dirloc, first_cluster, length, mode, name)
        if dirloc is None:
            dirloc = (0, 0)
        assert dirloc == (0, 0)
        if self.rootdir is not None:
            return self.rootdir
        d = _root_directory(self, dirloc, 0, length, "r+b", "/")
        l = d[0][2]
        if l != length:
            d.real_close()
            d = _root_directory(self, dirloc, 0, l, "r+b", "/")
        self.rootdir = d
        return d

    def _get_parent_dirloc(self, dirloc):
        """Get the dirloc of the parent of the entry referred to by dirloc."""
        cluster = self.read_allocatable_cluster(dirloc[0])
        ent = unpack_dirent(cluster[:PS2MC_DIRENT_LENGTH])
        return (ent[4], ent[5])

    def _dirloc_to_ent(self, dirloc):
        """Get the directory entry referred to by dirloc."""
        d = self._directory(
            None, dirloc[0], dirloc[1] + 1, name="_dirloc_to_ent temp"
        )
        ent = d[dirloc[1]]
        d.close()
        return ent

    def _opendir_dirloc(self, dirloc, mode="rb"):
        """Open the directory that is referred to by dirloc."""
        ent = self._dirloc_to_ent(dirloc)
        return self._directory(dirloc, ent[4], ent[2], name="_opendir_dirloc temp")

    def _opendir_parent_dirloc(self, dirloc, mode="rb"):
        """Open the directory that contains the entry referred to by dirloc."""
        return self._opendir_dirloc(self._get_parent_dirloc(dirloc), mode)

    def update_dirent_all(self, dirloc, thisf, new_ent):
        opened = self.open_files.get(dirloc, None)
        if opened is None:
            files = []
            d = None
        else:
            d, files = opened
        if d is None:
            d = self._opendir_parent_dirloc(dirloc, "r+b")
            if opened is not None:
                opened[0] = d

        ent = d[dirloc[1]]

        is_dir = ent[0] & DF_DIR

        if is_dir and thisf is not None and new_ent[2] is not None:
            new_ent = list(new_ent)
            new_ent[2] //= PS2MC_DIRENT_LENGTH

        modified = changed = notify = False
        for i in range(len(ent)):
            new = new_ent[i]
            if new is not None:
                if new != ent[i]:
                    ent[i] = new
                    changed = True
                    if i == 6:
                        modified = True
                    if i in (2, 4):
                        notify = True

        # Modifying a file causes the modification time of both the file
        # and the file's directory to be updated, however modifying a
        # directory never updates the modification time of the
        # directory's parent.
        if changed:
            d.write_raw_ent(dirloc[1], ent, (modified and not is_dir))

        if notify:
            for f in files:
                if f is not thisf:
                    f.update_notify(ent[4], ent[2])
        if opened is None:
            d.close()

    def update_dirent(self, dirloc, thisf, first_cluster, length, modified):
        if modified:
            modified = tod_now()
        else:
            if first_cluster is None and length is None:
                return
            modified = None
        self.update_dirent_all(
            dirloc,
            thisf,
            (None, None, length, None, first_cluster, None, modified, None, None),
        )

    def notify_closed(self, dirloc, thisf):
        if self.open_files is None or dirloc is None:
            return
        a = self.open_files.get(dirloc, None)
        if a is None:
            return
        self.flush()
        d, files = a
        files.discard(thisf)
        if len(files) == 0:
            if d is not None:
                d.close()
            del self.open_files[dirloc]

    def search_directory(self, d, name):
        """Search a directory for a name."""

        # start the search where the last search ended.
        start = d.tell() - 1
        if start == -1:
            start = 0
        for i in list(range(start, len(d))) + list(range(0, start)):
            try:
                ent = d[i]
            except IndexError:
                raise corrupt("Corrupt directory", d.f)

            if ent[8] == name and (ent[0] & DF_EXISTS):
                return (i, ent)
        return (None, None)

    def create_dir_entry(self, parent_dirloc, name, mode):
        """Create a new directory entry in a directory."""

        if name == "":
            raise file_not_found(name)

        dir_ent = self._dirloc_to_ent(parent_dirloc)
        d = self._directory(parent_dirloc, dir_ent[4], dir_ent[2], "r+b")
        l = len(d)
        assert l >= 2
        for i in range(l):
            ent = d[i]
            if (ent[0] & DF_EXISTS) == 0:
                break
        else:
            i = l

        dirloc = (dir_ent[4], i)
        now = tod_now()
        if mode & DF_DIR:
            mode &= ~DF_FILE
            cluster = self.allocate_cluster()
            if cluster is None:
                d.close()
                raise io_error(ENOSPC, "out of space on image", name)
            length = 1
        else:
            mode |= DF_FILE
            mode &= ~DF_DIR
            cluster = PS2MC_FAT_CHAIN_END
            length = 0
        ent[0] = mode | DF_EXISTS
        ent[1] = 0
        ent[2] = length
        ent[3] = now
        ent[4] = cluster
        ent[5] = 0
        ent[6] = now
        ent[7] = 0
        ent[8] = name[:32]
        d.write_raw_ent(i, ent, True)
        d.close()

        if mode & DF_FILE:
            return (dirloc, ent)

        dirent = pack_dirent(
            (
                DF_RWX | DF_0400 | DF_DIR | DF_EXISTS, 0, 0, now,
                dirloc[0], dirloc[1], now, 0, ".",
            )
        )
        dirent += b"\0" * (self.cluster_size - PS2MC_DIRENT_LENGTH)
        self.write_allocatable_cluster(cluster, dirent)
        d = self._directory(dirloc, cluster, 1, "wb", name="<create_dir_entry temp>")
        d.write_raw_ent(
            1,
            (DF_RWX | DF_0400 | DF_DIR | DF_EXISTS, 0, 0, now, 0, 0, now, 0, ".."),
            False,
        )
        d.close()
        ent[2] = 2
        return (dirloc, ent)

    def delete_dirloc(self, dirloc, truncate, name):
        """Delete or truncate the file or directory given by dirloc."""

        if dirloc == (0, 0):
            raise io_error(EACCES, "cannot remove root directory", name)
        if dirloc[1] in (0, 1):
            raise io_error(EACCES, 'cannot remove "." or ".." entries', name)

        if dirloc in self.open_files:
            raise io_error(EBUSY, "cannot remove open file", name)

        epc = self.entries_per_cluster

        ent = self._dirloc_to_ent(dirloc)
        cluster = ent[4]
        if truncate:
            ent[2] = 0
            ent[4] = PS2MC_FAT_CHAIN_END
            ent[6] = tod_now()
        else:
            ent[0] &= ~DF_EXISTS
        self.update_dirent_all(dirloc, None, ent)

        while cluster != PS2MC_FAT_CHAIN_END:
            if cluster // epc < self.fat_cursor:
                self.fat_cursor = cluster // epc
            next_cluster = self.lookup_fat(cluster)
            if next_cluster & PS2MC_FAT_ALLOCATED_BIT == 0:
                # corrupted
                break
            next_cluster &= ~PS2MC_FAT_ALLOCATED_BIT
            self.set_fat(cluster, next_cluster)
            if next_cluster == PS2MC_FAT_CHAIN_END_UNALLOC:
                break
            cluster = next_cluster

    def path_search(self, pathname):
        """Parse and resolve a pathname.

        Returns a tuple of three values.  The first is either the dirloc
        of the file or directory, if it exists, otherwise it's the dirloc
        of the pathname's parent directory, if that exists, otherwise it's
        None.  The second component is the directory entry for pathname if
        it exists, otherwise a dummy entry with the first element set to 0
        and the last element set to the final component of the pathname.
        The third is true if the pathname refers to a directory.
        """

        if pathname == "":
            return (None, None, False)

        (components, relative, is_dir) = pathname_split(pathname)

        dirloc = (0, 0)
        if relative:
            dirloc = self.curdir

        tmpname = "<path_search temp>"
        _directory = self._directory

        if dirloc == (0, 0):
            rootent = self.read_allocatable_cluster(0)
            ent = unpack_dirent(rootent[:PS2MC_DIRENT_LENGTH])
            dir_cluster = 0
            d = _directory(dirloc, dir_cluster, ent[2], name=tmpname)
        else:
            ent = self._dirloc_to_ent(dirloc)
            d = _directory(dirloc, ent[4], ent[2], name=tmpname)

        for s in components:
            if d is None:
                # tried to traverse a file or a non-existent directory
                return (None, (0, 0, 0, 0, 0, 0, 0, 0, None), False)

            if s == ".":
                continue
            if s == "..":
                dotent = d[0]
                d.close()
                dirloc = (dotent[4], dotent[5])
                ent = self._dirloc_to_ent(dirloc)
                d = _directory(dirloc, ent[4], ent[2], name=tmpname)
                continue

            dir_cluster = ent[4]
            (i, ent) = self.search_directory(d, s)
            d.close()
            d = None

            if ent is None:
                continue

            dirloc = (dir_cluster, i)
            if ent[0] & DF_DIR:
                d = _directory(dirloc, ent[4], ent[2], name=tmpname)

        if d is not None:
            d.close()
            is_dir = True
        elif ent is not None:
            is_dir = False

        if ent is None:
            ent = (0, 0, 0, 0, 0, 0, 0, 0, components[-1])

        return (dirloc, ent, is_dir)

    def open(self, filename, mode="r"):
        """Open a file, returning a new file-like object for it."""

        (dirloc, ent, is_dir) = self.path_search(filename)
        if dirloc is None:
            raise path_not_found(filename)
        if is_dir:
            raise io_error(EISDIR, "not a regular file", filename)
        if ent[0] == 0:
            if mode[0] not in "wa":
                raise file_not_found(filename)
            name = ent[8]
            (dirloc, ent) = self.create_dir_entry(
                dirloc, name, DF_FILE | DF_RWX | DF_0400
            )
            self.flush()
        elif mode[0] == "w":
            self.delete_dirloc(dirloc, True, filename)
            ent[4] = PS2MC_FAT_CHAIN_END
            ent[2] = 0
        return self.file(dirloc, ent[4], ent[2], mode, filename)

    def dir_open(self, filename, mode="rb"):
        (dirloc, ent, is_dir) = self.path_search(filename)
        if dirloc is None:
            raise path_not_found(filename)
        if ent[0] == 0:
            raise dir_not_found(filename)
        if not is_dir:
            raise io_error(ENOTDIR, "not a directory", filename)
        return self.directory(dirloc, ent[4], ent[2], mode, filename)

    def mkdir(self, filename):
        (dirloc, ent, is_dir) = self.path_search(filename)
        if dirloc is None:
            raise path_not_found(filename)
        if ent[0] != 0:
            raise io_error(EEXIST, "directory exists", filename)
        name = ent[8]
        self.create_dir_entry(dirloc, name, DF_DIR | DF_RWX | DF_0400)
        self.flush()

    def _is_empty(self, dirloc, ent, filename):
        """Check if a directory is empty."""
        d = self._directory(dirloc, ent[4], ent[2], "rb", filename)
        try:
            for i in range(2, len(d)):
                if d[i][0] & DF_EXISTS:
                    return False
        finally:
            d.close()
        return True

    def remove(self, filename):
        """Remove a file or empty directory."""

        (dirloc, ent, is_dir) = self.path_search(filename)
        if dirloc is None:
            raise path_not_found(filename)
        if ent[0] == 0:
            raise file_not_found(filename)
        if is_dir:
            if ent[4] == 0:
                raise io_error(EACCES, "cannot remove root directory", filename)
            if not self._is_empty(dirloc, ent, filename):
                raise io_error(ENOTEMPTY, "directory not empty", filename)
        self.delete_dirloc(dirloc, False, filename)
        self.flush()

    def chdir(self, filename):
        (dirloc, ent, is_dir) = self.path_search(filename)
        if dirloc is None:
            raise path_not_found(filename)
        if ent[0] == 0:
            raise dir_not_found(filename)
        if not is_dir:
            raise io_error(ENOTDIR, "not a directory", filename)
        self.curdir = dirloc

    def get_mode(self, filename):
        """Get the mode bits of a file.

        Returns None if the filename doesn't exist, rather than raising.
        """
        (dirloc, ent, is_dir) = self.path_search(filename)
        if ent is None or ent[0] == 0:
            return None
        return ent[0]

    def get_dirent(self, filename):
        """Get the raw directory entry list for a file."""
        (dirloc, ent, is_dir) = self.path_search(filename)
        if dirloc is None:
            raise path_not_found(filename)
        if ent[0] == 0:
            raise file_not_found(filename)
        return ent

    def set_dirent(self, filename, new_ent):
        """Set various directory entry fields of a file.

        Not all fields can be changed.  A field set to None in new_ent is
        left unchanged.
        """
        (dirloc, ent, is_dir) = self.path_search(filename)
        if dirloc is None:
            raise path_not_found(filename)
        if ent[0] == 0:
            raise file_not_found(filename)
        d = self._opendir_parent_dirloc(dirloc, "r+b")
        try:
            new_ent = list(new_ent)
            new_ent[8] = None
            d[dirloc[1]] = new_ent
        finally:
            d.close()
        self.flush()
        return ent

    def is_ancestor(self, dirloc, olddirloc):
        while True:
            if dirloc == olddirloc:
                return True
            if dirloc == (0, 0):
                return False
            dirloc = self._get_parent_dirloc(dirloc)

    def rename(self, oldpathname, newpathname):
        (olddirloc, oldent, is_dir) = self.path_search(oldpathname)
        if olddirloc is None:
            raise path_not_found(oldpathname)
        if oldent[0] == 0:
            raise file_not_found(oldpathname)

        if olddirloc == (0, 0):
            raise io_error(EINVAL, "cannot rename root directory", oldpathname)
        if olddirloc in self.open_files:
            raise io_error(EBUSY, "cannot rename open file", oldpathname)

        (newparentdirloc, newent, x) = self.path_search(newpathname)
        if newparentdirloc is None:
            raise path_not_found(newpathname)
        if newent[0] != 0:
            raise io_error(EEXIST, "file exists", newpathname)
        newname = newent[8]

        oldparentdirloc = self._get_parent_dirloc(olddirloc)
        if oldparentdirloc == newparentdirloc:
            d = self._opendir_dirloc(oldparentdirloc, "r+b")
            try:
                d[olddirloc[1]] = (
                    None, None, None, None, None, None, None, None, newname,
                )
            finally:
                d.close()
            self.flush()
            return

        if is_dir and self.is_ancestor(newparentdirloc, olddirloc):
            raise io_error(
                EINVAL, "cannot move directory beneath itself", oldpathname
            )

        newparentdir = None
        newent = None
        newdirloc = None
        try:
            tmpmode = (oldent[0] & ~DF_DIR) | DF_FILE

            (newdirloc, newent) = self.create_dir_entry(
                newparentdirloc, newname, tmpmode
            )

            newent[:8] = oldent[:8]
            newparentdir = self._opendir_dirloc(newparentdirloc)
            newparentdir.write_raw_ent(newdirloc[1], newent, True)
            newent = None

            oldent[0] &= ~DF_EXISTS
            self.update_dirent_all(olddirloc, None, oldent)

        except Exception:
            if newent is not None and newdirloc is not None:
                self.delete_dirloc(newdirloc, False, newpathname)
            raise
        finally:
            if newparentdir is not None:
                newparentdir.close()

        if not is_dir:
            self.flush()
            return

        newdir = self._opendir_dirloc(newdirloc)
        try:
            dotent = list(newdir[0])
            dotent[4:6] = newdirloc
            newdir.write_raw_ent(0, dotent, False)
        finally:
            newdir.close()
        self.flush()

    def import_save_file(self, sf, ignore_existing, dirname=None):
        """Copy the contents of a ps2_save_file object to a directory.

        If ignore_existing is true and the directory being imported to
        already exists then False is returned instead of raising an error.
        If dirname is given then the save file is copied to that directory
        instead of the directory specified by the save file.
        """

        dir_ent = sf.get_directory()
        if dirname is None:
            dirname = "/" + dir_ent[8]

        (root_dirloc, ent, is_dir) = self.path_search(dirname)
        if root_dirloc is None:
            raise path_not_found(dirname)
        if ent[0] != 0:
            if ignore_existing:
                return False
            raise io_error(EEXIST, "directory exists", dirname)
        name = ent[8]
        mode = DF_DIR | (dir_ent[0] & ~DF_FILE)

        (dir_dirloc, ent) = self.create_dir_entry(root_dirloc, name, mode)
        try:
            assert dirname != "/"
            dirname = dirname + "/"
            for i in range(dir_ent[2]):
                (ent, data) = sf.get_file(i)
                mode = DF_FILE | (ent[0] & ~DF_DIR)
                (dirloc, ent) = self.create_dir_entry(dir_dirloc, ent[8], mode)
                f = self.file(dirloc, ent[4], ent[2], "wb", dirname + ent[8])
                try:
                    f.write(data)
                finally:
                    f.close()
        except OSError:
            # roll back the partial import, then report the original error
            try:
                for i in range(dir_ent[2]):
                    (ent, data) = sf.get_file(i)
                    try:
                        self.remove(dirname + ent[8])
                    except OSError:
                        pass
                try:
                    self.remove(dirname)
                except OSError:
                    pass
            except Exception:
                pass
            raise

        # set modes and timestamps to those of the save file

        d = self._opendir_dirloc(dir_dirloc, "r+b")
        try:
            for i in range(dir_ent[2]):
                d[i + 2] = sf.get_file(i)[0]
        finally:
            d.close()

        d = self._opendir_dirloc(root_dirloc, "r+b")
        try:
            a = dir_ent[:]
            a[8] = None  # don't change the name
            d[dir_dirloc[1]] = a
        finally:
            d.close()

        self.flush()
        return True

    def export_save_file(self, filename, log=None):
        (dir_dirloc, dirent, is_dir) = self.path_search(filename)
        if dir_dirloc is None:
            raise path_not_found(filename)
        if dirent[0] == 0:
            raise dir_not_found(filename)
        if not is_dir:
            raise io_error(ENOTDIR, "not a directory", filename)
        if dir_dirloc == (0, 0):
            raise io_error(EACCES, "can't export root directory", filename)
        sf = ps2save.ps2_save_file()
        files = []
        f = None
        d = self._directory(dir_dirloc, dirent[4], dirent[2], "rb", filename)
        try:
            for i in range(2, dirent[2]):
                ent = d[i]
                if not mode_is_file(ent[0]):
                    if log is not None:
                        log(
                            "warning: %s/%s is not a file, ignored."
                            % (dirent[8], ent[8])
                        )
                    continue
                f = self.file((dirent[4], i), ent[4], ent[2], "rb")
                data = f.read(ent[2])
                f.close()
                f = None
                assert len(data) == ent[2]
                files.append((ent, data))
        finally:
            if f is not None:
                f.close()
            d.close()
        dirent[2] = len(files)
        sf.set_directory(dirent)
        for (i, (ent, data)) in enumerate(files):
            sf.set_file(i, ent, data)
        return sf

    def _remove_dir(self, dirloc, ent, dirname):
        """Recurse over a directory tree to remove it.

        If not "", dirname must end with a slash (/).
        """
        first_cluster = ent[4]
        length = ent[2]
        d = self._directory(dirloc, first_cluster, length, "rb", dirname)
        try:
            ents = list(enumerate(d))
        finally:
            d.close()
        for (i, ent) in ents[2:]:
            mode = ent[0]
            if not (mode & DF_EXISTS):
                continue
            if mode & DF_DIR:
                self._remove_dir(
                    (first_cluster, i), ent, dirname + ent[8] + "/"
                )
            else:
                self.delete_dirloc((first_cluster, i), False, dirname + ent[8])
        self.delete_dirloc(dirloc, False, dirname)

    def rmdir(self, dirname):
        """Recursively delete a directory."""

        (dirloc, ent, is_dir) = self.path_search(dirname)
        if dirloc is None:
            raise path_not_found(dirname)
        if ent[0] == 0:
            raise dir_not_found(dirname)
        if not is_dir:
            raise io_error(ENOTDIR, "not a directory", dirname)
        if dirloc == (0, 0):
            raise io_error(EACCES, "can't delete root directory", dirname)

        if dirname != "" and dirname[-1] != "/":
            dirname += "/"
        self._remove_dir(dirloc, ent, dirname)
        self.flush()

    def get_free_space(self):
        """Return the amount of free space in bytes."""
        free = 0
        epc = self.entries_per_cluster
        end = self.allocatable_cluster_end
        for base in range(0, end, epc):
            (fat, cluster) = self.read_fat_cluster(base // epc)
            for value in fat[: min(epc, end - base)]:
                if (value & PS2MC_FAT_ALLOCATED_BIT) == 0:
                    free += 1
        return free * self.cluster_size

    def get_allocatable_space(self):
        """Return the total amount of allocatable space in bytes."""
        return self.allocatable_cluster_limit * self.cluster_size

    def _check_file(self, fat, first_cluster, length):
        cluster = first_cluster
        i = 0
        while cluster != PS2MC_FAT_CHAIN_END:
            if cluster < 0 or cluster >= len(fat):
                return "invalid cluster in chain"
            if fat[cluster]:
                return "cross linked chain"
            i += 1
            fat[cluster] = 1
            nxt = self.lookup_fat(cluster)
            if nxt == PS2MC_FAT_CHAIN_END:
                break
            if (nxt & PS2MC_FAT_ALLOCATED_BIT) == 0:
                return "unallocated cluster in chain"
            cluster = nxt & ~PS2MC_FAT_ALLOCATED_BIT
        file_cluster_end = div_round_up(length, self.cluster_size)
        if i < file_cluster_end:
            return "chain ends before end of file"
        elif i > file_cluster_end:
            return "chain continues after end of file"
        return None

    def _check_dir(self, fat, dirloc, dirname, ent, log):
        why = self._check_file(fat, ent[4], ent[2] * PS2MC_DIRENT_LENGTH)
        if why is not None:
            log("bad directory: " + dirname + ": " + why)
            return False
        ret = True
        first_cluster = ent[4]
        length = ent[2]
        d = self._directory(dirloc, first_cluster, length, "rb", dirname)
        dot_ent = d[0]
        if dot_ent[8] != ".":
            log("bad directory: " + dirname + ': missing "." entry')
            ret = False
        if (dot_ent[4], dot_ent[5]) != dirloc:
            log("bad directory: " + dirname + ': bad "." entry')
            ret = False
        if d[1][8] != "..":
            log("bad directory: " + dirname + ': missing ".." entry')
            ret = False
        for i in range(2, length):
            ent = d[i]
            mode = ent[0]
            if not (mode & DF_EXISTS):
                continue
            if mode & DF_DIR:
                if not self._check_dir(
                    fat, (first_cluster, i), dirname + ent[8] + "/", ent, log
                ):
                    ret = False
            else:
                why = self._check_file(fat, ent[4], ent[2])
                if why is not None:
                    log("bad file: " + dirname + ent[8] + ": " + why)
                    ret = False

        d.close()
        return ret

    def check(self, log=None):
        """Run a simple file system check.

        Any problems found are reported through the log callback, which
        defaults to printing to standard output.  Returns True when the
        image is free of the errors this check can spot.
        """
        if log is None:
            log = print

        fat = bytearray(self.allocatable_cluster_end)

        cluster = self.read_allocatable_cluster(0)
        ent = unpack_dirent(cluster[:PS2MC_DIRENT_LENGTH])
        ret = self._check_dir(fat, (0, 0), "/", ent, log)

        lost = []
        for i in range(self.allocatable_cluster_end):
            a = self.lookup_fat(i)
            if (a & PS2MC_FAT_ALLOCATED_BIT) and not fat[i]:
                lost.append(i)
        if lost:
            log("lost clusters: " + " ".join(str(i) for i in lost))
            log("found %d lost clusters" % len(lost))
            ret = False

        return ret

    def _globdir(self, dirname, components, is_dir):
        pattern = components[0]
        d = self.dir_open("." if dirname == "" else dirname)
        try:
            return [
                dirname + ent[8]
                for ent in d
                if (
                    (ent[0] & DF_EXISTS)
                    and (not is_dir or (ent[0] & DF_DIR))
                    and (ent[8] not in (".", "..") or ent[8] == pattern)
                    and fnmatch.fnmatchcase(ent[8], pattern)
                )
            ]
        finally:
            d.close()

    def _glob(self, dirname, components, is_dir):
        pattern = components[0]
        components = components[1:]

        if len(components) == 1:
            _globfn = self._globdir
        else:
            _globfn = self._glob

        d = self.dir_open("." if dirname == "" else dirname)
        try:
            ret = []
            for ent in d:
                name = ent[8]
                if (ent[0] & DF_EXISTS) == 0 or (ent[0] & DF_DIR) == 0:
                    continue
                if name == "." or name == "..":
                    if pattern != name:
                        continue
                elif not fnmatch.fnmatchcase(name, pattern):
                    continue
                ret += _globfn(dirname + name + "/", components, is_dir)
        finally:
            d.close()
        return ret

    def glob(self, pattern):
        if pattern == "":
            return [""]
        (components, relative, isdir) = pathname_split(pattern)
        if len(components) == 0:
            return ["/"]
        dirname = "" if relative else "/"
        if len(components) == 1:
            return self._globdir(dirname, components, isdir)
        return self._glob(dirname, components, isdir)

    def get_icon_sys(self, dirname):
        """Get the contents of a directory's icon.sys file, if it exists."""

        icon_sys = dirname + "/icon.sys"
        mode = self.get_mode(icon_sys)
        if mode is None or not mode_is_file(mode):
            return None
        f = self.open(icon_sys, "rb")
        s = f.read(ps2save.ICON_SYS_LENGTH)
        f.close()
        if len(s) == ps2save.ICON_SYS_LENGTH and s[0:4] == b"PS2D":
            return s
        return None

    def dir_size(self, dirname):
        """Calculate the total size of the contents of a directory."""

        d = self.dir_open(dirname)
        try:
            length = round_up(len(d) * PS2MC_DIRENT_LENGTH, self.cluster_size)
            for ent in d:
                if mode_is_file(ent[0]):
                    length += round_up(ent[2], self.cluster_size)
                elif mode_is_dir(ent[0]) and ent[8] not in (".", ".."):
                    length += self.dir_size(dirname + "/" + ent[8])
        finally:
            d.close()
        return length

    def flush(self):
        self.flush_alloc_cluster_cache()
        self.flush_fat_cache()
        if self.modified:
            self.write_superblock()
        self.f.flush()

    def close(self):
        """Close all open files.

        Disconnects, but doesn't close, the file object used to access the
        raw image.  After this method has been called on a ps2mc object, it
        can no longer be used.
        """
        try:
            f = self.f
            if f is None or getattr(f, "closed", False):
                return
            open_files = self.open_files
            if open_files is not None:
                # this is complicated by the fact that as files are
                # closed they remove themselves from the list of open
                # files
                for (d, files) in list(open_files.values()):
                    for f in list(files):
                        f.close()
                while len(open_files) > 0:
                    (k, v) = open_files.popitem()
                    (d, files) = v
                    if d is not None:
                        d.close()
            if self.rootdir is not None:
                self.rootdir.real_close()
            if self.fat_cache is not None:
                self.flush()
        finally:
            self.open_files = None
            self.fat_cache = None
            self.f = None
            self.rootdir = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            sys.stderr.write("ps2mc.__del__: \n")
            traceback.print_exc()
