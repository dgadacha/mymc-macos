"""Tests for the memory card file system and the save file formats."""

import io
import os
import random

import pytest

from conftest import STANDARD_PARAMS, make_icon_sys, make_save
from mymc import lzari, ps2mc, ps2mc_ecc, ps2save
from mymc.ps2mc_dir import (
    DF_DIR,
    DF_EXISTS,
    DF_RWX,
    mode_is_dir,
    mode_is_file,
    pack_dirent,
    tod_now,
    tod_to_time,
    unpack_dirent,
)
from mymc.rounding import div_round_up, round_down, round_up


class TestRounding:
    def test_results_are_integers(self):
        for fn in (div_round_up, round_up, round_down):
            assert isinstance(fn(7, 3), int)

    def test_values(self):
        assert div_round_up(7, 3) == 3
        assert div_round_up(6, 3) == 2
        assert round_up(7, 3) == 9
        assert round_down(7, 3) == 6


class TestDirent:
    def test_round_trip(self):
        ent = [
            DF_RWX | DF_DIR | DF_EXISTS, 0, 2, tod_now(),
            5, 1, tod_now(), 0, "BASLUS-20678",
        ]
        raw = pack_dirent(ent)
        assert len(raw) == 512
        assert unpack_dirent(raw) == ent

    def test_non_ascii_name_survives(self):
        # names are mapped through latin-1, so every byte round trips
        ent = [DF_RWX | DF_DIR | DF_EXISTS, 0, 2, tod_now(), 0, 0,
               tod_now(), 0, "".join(chr(c) for c in range(1, 33))]
        assert unpack_dirent(pack_dirent(ent))[8] == ent[8]

    def test_tod_round_trip(self):
        import time

        now = time.time()
        assert abs(tod_to_time(tod_now()) - now) < 2


class TestEcc:
    def test_fast_and_slow_paths_agree(self):
        if not ps2mc_ecc.have_fast_ecc:
            pytest.skip("NumPy not installed")
        rng = random.Random(1)
        for _ in range(20):
            page = bytes(rng.randrange(256) for _ in range(512))
            assert ps2mc_ecc._ecc_calculate_page_np(
                page
            ) == ps2mc_ecc._ecc_calculate_page_py(page)

    def test_clean_page(self):
        page = bytes(range(256)) * 2
        spare = ps2mc_ecc.ecc_calculate_page(page) + b"\0" * 4
        status, out, _ = ps2mc_ecc.ecc_check_page(page, spare)
        assert status == ps2mc_ecc.ECC_CHECK_OK
        assert out == page

    def test_single_bit_error_is_corrected(self):
        rng = random.Random(2)
        page = bytes(rng.randrange(256) for _ in range(512))
        spare = ps2mc_ecc.ecc_calculate_page(page) + b"\0" * 4
        for offset, bit in ((0, 0), (70, 3), (511, 7), (128, 5)):
            bad = bytearray(page)
            bad[offset] ^= 1 << bit
            status, out, _ = ps2mc_ecc.ecc_check_page(bytes(bad), spare)
            assert status == ps2mc_ecc.ECC_CHECK_CORRECTED
            assert out == page

    def test_two_bit_error_is_detected(self):
        rng = random.Random(3)
        page = bytes(rng.randrange(256) for _ in range(512))
        spare = ps2mc_ecc.ecc_calculate_page(page) + b"\0" * 4
        bad = bytearray(page)
        bad[10] ^= 0x01
        bad[90] ^= 0x40
        status, _, _ = ps2mc_ecc.ecc_check_page(bytes(bad), spare)
        assert status == ps2mc_ecc.ECC_CHECK_FAILED


class TestLzari:
    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"A",
            b"MEMORY CARD DATA " * 40,
            bytes(3000),
            bytes(range(256)) * 8,
        ],
    )
    def test_round_trip(self, data):
        compressed = lzari.encode(data)
        assert lzari.decode(compressed, len(data)) == data

    def test_random_round_trips(self):
        rng = random.Random(1234)
        for trial in range(12):
            n = rng.randrange(1, 3000)
            if trial % 2:
                data = bytes(rng.randrange(256) for _ in range(n))
            else:
                data = bytes(rng.randrange(4) for _ in range(n))
            compressed = lzari.encode(data)
            assert lzari.decode(compressed, len(data)) == data

    def test_compresses_repetitive_data(self):
        data = b"SAVE" * 500
        assert len(lzari.encode(data)) < len(data) // 10

    def test_bit_array_round_trip(self):
        rng = random.Random(5)
        for n in (0, 1, 5, 17, 256):
            s = bytes(rng.randrange(256) for _ in range(n))
            bits = lzari.string_to_bit_array(s)
            assert len(bits) == n * 8
            assert lzari.bit_array_to_string(bits) == s


class TestFilesystem:
    def test_fresh_card_is_clean(self, blank_card):
        assert blank_card.check(log=lambda m: None)
        assert blank_card.get_free_space() == 8134 * 1024

    def test_image_size_matches_a_real_card(self):
        buf = io.BytesIO()
        buf.name = "x.ps2"
        ps2mc.ps2mc(buf, params=STANDARD_PARAMS).close()
        # 16384 pages of 512 bytes plus 16 bytes of spare area each
        assert len(buf.getvalue()) == 16384 * 528

    def test_file_round_trip_across_clusters(self, blank_card):
        data = bytes(range(256)) * 30  # 7680 bytes, several clusters
        blank_card.mkdir("/BASLUS-20678")
        f = blank_card.open("/BASLUS-20678/game.dat", "wb")
        f.write(data)
        f.close()
        f = blank_card.open("/BASLUS-20678/game.dat", "rb")
        assert f.read() == data
        f.close()
        assert blank_card.check(log=lambda m: None)

    def test_seek_and_partial_reads(self, blank_card):
        data = bytes(range(256)) * 10
        f = blank_card.open("/data.bin", "wb")
        f.write(data)
        f.close()
        f = blank_card.open("/data.bin", "rb")
        f.seek(1000)
        assert f.read(100) == data[1000:1100]
        f.seek(-10, 2)
        assert f.read() == data[-10:]
        f.close()

    def test_persists_across_reopen(self):
        buf = io.BytesIO()
        buf.name = "x.ps2"
        mc = ps2mc.ps2mc(buf, params=STANDARD_PARAMS)
        mc.mkdir("/DIR")
        f = mc.open("/DIR/a", "wb")
        f.write(b"hello")
        f.close()
        mc.close()

        buf.seek(0)
        mc = ps2mc.ps2mc(buf)
        f = mc.open("/DIR/a", "rb")
        assert f.read() == b"hello"
        f.close()
        mc.close()

    def test_mkdir_rmdir_and_glob(self, blank_card):
        blank_card.mkdir("/BASLUS-1")
        blank_card.mkdir("/BASLUS-2")
        blank_card.mkdir("/BEDATA-1")
        assert sorted(blank_card.glob("/BASLUS*")) == ["/BASLUS-1", "/BASLUS-2"]
        blank_card.rmdir("/BASLUS-1")
        assert blank_card.glob("/BASLUS*") == ["/BASLUS-2"]
        assert blank_card.check(log=lambda m: None)

    def test_rename(self, blank_card):
        blank_card.mkdir("/OLD")
        blank_card.rename("/OLD", "/NEW")
        assert blank_card.get_mode("/NEW") is not None
        assert blank_card.get_mode("/OLD") is None

    def test_remove_non_empty_directory_fails(self, blank_card):
        blank_card.mkdir("/DIR")
        f = blank_card.open("/DIR/a", "wb")
        f.write(b"x")
        f.close()
        with pytest.raises(OSError):
            blank_card.remove("/DIR")

    def test_missing_file_raises(self, blank_card):
        with pytest.raises(OSError):
            blank_card.open("/nope", "rb")

    def test_not_a_card_image(self):
        buf = io.BytesIO(b"definitely not a memory card")
        buf.name = "junk.bin"
        with pytest.raises(ps2mc.error):
            ps2mc.ps2mc(buf)

    def test_context_manager(self):
        buf = io.BytesIO()
        buf.name = "x.ps2"
        with ps2mc.ps2mc(buf, params=STANDARD_PARAMS) as mc:
            mc.mkdir("/X")
        assert mc.f is None

    def test_out_of_space_is_reported(self):
        # a deliberately tiny card: 512 KB
        buf = io.BytesIO()
        buf.name = "small.ps2"
        mc = ps2mc.ps2mc(buf, params=(True, 512, 16, 1024))
        with pytest.raises(OSError):
            f = mc.open("/big", "wb")
            for _ in range(2048):
                f.write(b"\0" * 1024)
            f.close()
        mc.close()


class TestSaveFormats:
    def test_psu_round_trip(self):
        sf, files = make_save()
        buf = io.BytesIO()
        sf.save_ems(buf)
        buf.seek(0)
        assert ps2save.detect_file_type(buf) == "psu"
        buf.seek(0)
        loaded = ps2save.load_save_file(buf)
        assert [(loaded[i][0][8], loaded[i][1]) for i in range(len(loaded))] == files

    def test_max_round_trip(self, quiet):
        sf, files = make_save()
        buf = io.BytesIO()
        sf.save_max_drive(buf, progress=quiet)
        buf.seek(0)
        assert ps2save.detect_file_type(buf) == "max"
        buf.seek(0)
        loaded = ps2save.load_save_file(buf)
        loaded.decompress(quiet)
        assert [(loaded[i][0][8], loaded[i][1]) for i in range(len(loaded))] == files

    def test_psu_to_max_to_psu_preserves_the_files(self, quiet):
        # A MAX Drive file carries no timestamps, so the round trip cannot
        # be byte identical -- but every file name and byte of content has
        # to survive, and so does the save's directory name.
        sf, files = make_save()
        first = io.BytesIO()
        sf.save_ems(first)

        first.seek(0)
        mid = io.BytesIO()
        ps2save.load_save_file(first).save_max_drive(mid, progress=quiet)

        mid.seek(0)
        again = ps2save.load_save_file(mid)
        again.decompress(quiet)
        last = io.BytesIO()
        again.save_ems(last)

        last.seek(0)
        final = ps2save.load_save_file(last)
        assert final.get_directory()[8] == sf.get_directory()[8]
        assert [(final[i][0][8], final[i][1]) for i in range(len(final))] == files

    def test_max_drive_regenerates_timestamps(self, quiet):
        # The format carries no timestamps, so loading one stamps it with
        # the current time.  Documented here so a future change to the
        # timestamp handling does not go unnoticed.
        import time

        sf, _ = make_save()
        buf = io.BytesIO()
        sf.save_max_drive(buf, progress=quiet)
        buf.seek(0)
        reloaded = ps2save.load_save_file(buf)
        stamped = tod_to_time(reloaded.get_directory()[3])
        assert abs(stamped - time.time()) < 5

        # An explicit timestamp can be supplied instead.
        buf.seek(0)
        chosen = (0, 30, 12, 25, 12, 2003)
        other = ps2save.ps2_save_file()
        other.load_max_drive(buf, timestamp=chosen)
        assert other.get_directory()[3] == chosen

    def test_unrecognised_format(self):
        with pytest.raises(ps2save.error):
            ps2save.load_save_file(io.BytesIO(b"nonsense" * 300))

    def test_nport_is_rejected_by_name(self):
        with pytest.raises(ps2save.error, match="nPort"):
            ps2save.load_save_file(io.BytesIO(b"nPort" + b"\0" * 2000))

    def test_icon_sys_title(self):
        raw = make_icon_sys("UNLIMITED SAGA", "SYSTEMDATA")
        icon_sys = ps2save.unpack_icon_sys(raw)
        assert ps2save.icon_sys_title(icon_sys) == ("UNLIMITED SAGA", "SYSTEMDATA")

    def test_japanese_title_transliteration(self):
        raw = make_icon_sys("ＴＥＳＴ　１２３", "【あ】")
        icon_sys = ps2save.unpack_icon_sys(raw)
        line1, line2 = ps2save.icon_sys_title(icon_sys, "ascii")
        assert line1 == "TEST 123"
        assert line2.startswith("[") and line2.endswith("]")

    def test_make_longname(self):
        sf, _ = make_save("BASLUS-20678SAVE")
        name = ps2save.make_longname("BASLUS-20678SAVE", sf)
        assert name.startswith("SLUS-20678")
        assert "UNLIMITED SAGA" in name
        assert "/" not in name

    def test_rc4_is_its_own_inverse(self):
        data = bytes(range(256))
        once = bytes(ps2save.rc4_crypt(ps2save.PS2SAVE_CBS_RC4S, data))
        twice = bytes(ps2save.rc4_crypt(ps2save.PS2SAVE_CBS_RC4S, once))
        assert once != data
        assert twice == data


class TestImportExport:
    def test_import_then_export_is_lossless(self, blank_card):
        sf, files = make_save()
        assert blank_card.import_save_file(sf, False)
        assert blank_card.check(log=lambda m: None)

        out = blank_card.export_save_file("/BASLUS-20678SAVE")
        assert [(out[i][0][8], out[i][1]) for i in range(len(out))] == files

    def test_duplicate_import_is_ignored(self, blank_card):
        sf, _ = make_save()
        assert blank_card.import_save_file(sf, False)
        assert blank_card.import_save_file(sf, True) is False

    def test_duplicate_import_raises_without_ignore(self, blank_card):
        sf, _ = make_save()
        blank_card.import_save_file(sf, False)
        with pytest.raises(OSError):
            blank_card.import_save_file(sf, False)

    def test_icon_sys_readable_from_card(self, blank_card):
        sf, _ = make_save()
        blank_card.import_save_file(sf, False)
        raw = blank_card.get_icon_sys("/BASLUS-20678SAVE")
        assert raw is not None
        title = ps2save.icon_sys_title(ps2save.unpack_icon_sys(raw))
        assert title == ("UNLIMITED SAGA", "SYSTEMDATA")

    def test_free_space_accounting(self, blank_card):
        before = blank_card.get_free_space()
        sf, files = make_save()
        blank_card.import_save_file(sf, False)
        after = blank_card.get_free_space()
        assert after < before
        blank_card.rmdir("/BASLUS-20678SAVE")
        assert blank_card.get_free_space() > after
