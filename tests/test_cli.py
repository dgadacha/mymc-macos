"""End to end tests driving the command line interface."""

import os

import pytest

from conftest import make_save
from mymc import cli, ps2save


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(*args):
    return cli.main(list(args))


def write_psu(path, dirname="BASLUS-20678SAVE"):
    sf, files = make_save(dirname)
    with open(path, "wb") as f:
        sf.save_ems(f)
    return files


class TestCli:
    def test_format_creates_a_card(self, workdir, capsys):
        assert run("Mcd001.ps2", "format") == 0
        assert os.path.getsize("Mcd001.ps2") == 16384 * 528
        assert "Formatted" in capsys.readouterr().out

    def test_format_refuses_to_clobber(self, workdir):
        run("Mcd001.ps2", "format")
        assert run("Mcd001.ps2", "format") == 1
        assert run("Mcd001.ps2", "format", "-f") == 0

    def test_format_without_ecc_is_smaller(self, workdir):
        run("plain.ps2", "format", "-e")
        assert os.path.getsize("plain.ps2") == 16384 * 512

    def test_check_and_df(self, workdir, capsys):
        run("Mcd001.ps2", "format")
        capsys.readouterr()
        assert run("Mcd001.ps2", "check") == 0
        assert "No errors found" in capsys.readouterr().out
        assert run("Mcd001.ps2", "df") == 0
        assert "bytes free" in capsys.readouterr().out

    def test_import_dir_export_round_trip(self, workdir, capsys):
        files = write_psu("save.psu")
        run("Mcd001.ps2", "format")
        assert run("Mcd001.ps2", "import", "save.psu") == 0

        capsys.readouterr()
        assert run("Mcd001.ps2", "dir") == 0
        out = capsys.readouterr().out
        assert "BASLUS-20678SAVE" in out
        assert "UNLIMITED SAGA" in out
        assert "KB Free" in out

        os.mkdir("out")
        assert run("Mcd001.ps2", "export", "-d", "out", "BASLUS-20678SAVE") == 0
        exported = os.path.join("out", "BASLUS-20678SAVE.psu")
        assert os.path.exists(exported)
        with open(exported, "rb") as f:
            sf = ps2save.load_save_file(f)
        assert [(sf[i][0][8], sf[i][1]) for i in range(len(sf))] == files

    def test_export_max_and_reimport(self, workdir, capsys):
        files = write_psu("save.psu")
        run("Mcd001.ps2", "format")
        run("Mcd001.ps2", "import", "save.psu")
        assert run("Mcd001.ps2", "export", "-m", "BASLUS-20678SAVE") == 0
        assert os.path.exists("BASLUS-20678SAVE.max")

        run("card2.ps2", "format")
        assert run("card2.ps2", "import", "BASLUS-20678SAVE.max") == 0
        capsys.readouterr()
        run("card2.ps2", "ls", "/BASLUS-20678SAVE")
        listing = capsys.readouterr().out
        for name, _ in files:
            assert name in listing

    def test_export_long_names(self, workdir):
        write_psu("save.psu")
        run("Mcd001.ps2", "format")
        run("Mcd001.ps2", "import", "save.psu")
        assert run("Mcd001.ps2", "export", "-l", "BASLUS-20678SAVE") == 0
        names = [n for n in os.listdir(".") if n.endswith(".psu")]
        assert any("UNLIMITED SAGA" in n for n in names)

    def test_export_refuses_to_overwrite(self, workdir):
        write_psu("save.psu")
        run("Mcd001.ps2", "format")
        run("Mcd001.ps2", "import", "save.psu")
        assert run("Mcd001.ps2", "export", "BASLUS-20678SAVE") == 0
        assert run("Mcd001.ps2", "export", "BASLUS-20678SAVE") == 1
        assert run("Mcd001.ps2", "export", "-f", "BASLUS-20678SAVE") == 0
        assert run("Mcd001.ps2", "export", "-i", "BASLUS-20678SAVE") == 0

    def test_duplicate_import_is_reported(self, workdir, capsys):
        write_psu("save.psu")
        run("Mcd001.ps2", "format")
        run("Mcd001.ps2", "import", "save.psu")
        capsys.readouterr()
        run("Mcd001.ps2", "import", "-i", "save.psu")
        assert "already in memory card image" in capsys.readouterr().out

    def test_add_extract_and_remove(self, workdir, capsys):
        run("Mcd001.ps2", "format")
        run("Mcd001.ps2", "mkdir", "/MYDIR")
        with open("note.txt", "wb") as f:
            f.write(b"hello from macOS\n")
        assert run("Mcd001.ps2", "add", "-d", "/MYDIR", "note.txt") == 0

        os.remove("note.txt")
        assert run("Mcd001.ps2", "extract", "-d", "/MYDIR", "note.txt") == 0
        with open("note.txt", "rb") as f:
            assert f.read() == b"hello from macOS\n"

        assert run("Mcd001.ps2", "remove", "/MYDIR/note.txt") == 0
        capsys.readouterr()
        run("Mcd001.ps2", "ls", "/MYDIR")
        assert "note.txt" not in capsys.readouterr().out

    def test_extract_to_stdout(self, workdir, capsysbinary):
        run("Mcd001.ps2", "format")
        with open("note.txt", "wb") as f:
            f.write(b"piped")
        run("Mcd001.ps2", "add", "note.txt")
        capsysbinary.readouterr()
        run("Mcd001.ps2", "extract", "-p", "/note.txt")
        assert b"piped" in capsysbinary.readouterr().out

    def test_rename_and_delete(self, workdir, capsys):
        write_psu("save.psu")
        run("Mcd001.ps2", "format")
        run("Mcd001.ps2", "import", "save.psu")
        assert run("Mcd001.ps2", "rename", "/BASLUS-20678SAVE", "/RENAMED") == 0
        capsys.readouterr()
        run("Mcd001.ps2", "ls", "/")
        assert "RENAMED" in capsys.readouterr().out

        assert run("Mcd001.ps2", "delete", "/RENAMED") == 0
        capsys.readouterr()
        run("Mcd001.ps2", "ls", "/")
        assert "RENAMED" not in capsys.readouterr().out
        assert run("Mcd001.ps2", "check") == 0

    def test_set_and_clear_mode_flags(self, workdir, capsys):
        write_psu("save.psu")
        run("Mcd001.ps2", "format")
        run("Mcd001.ps2", "import", "save.psu")
        assert run("Mcd001.ps2", "set", "-p", "/BASLUS-20678SAVE") == 0
        capsys.readouterr()
        run("Mcd001.ps2", "dir")
        assert "Copy Protected" in capsys.readouterr().out
        assert run("Mcd001.ps2", "clear", "-p", "/BASLUS-20678SAVE") == 0
        capsys.readouterr()
        run("Mcd001.ps2", "dir")
        assert "Not Protected" in capsys.readouterr().out

    def test_missing_image_reports_cleanly(self, workdir, capsys):
        assert run("nope.ps2", "dir") == 1
        assert "nope.ps2" in capsys.readouterr().err

    def test_corrupt_image_reports_cleanly(self, workdir, capsys):
        with open("junk.ps2", "wb") as f:
            f.write(b"not a memory card" * 100)
        assert run("junk.ps2", "dir") == 1
        assert "junk.ps2" in capsys.readouterr().err

    def test_unknown_save_format_is_reported(self, workdir, capsys):
        run("Mcd001.ps2", "format")
        with open("bogus.psu", "wb") as f:
            f.write(b"\0" * 4096)
        assert run("Mcd001.ps2", "import", "bogus.psu") == 1
        assert "bogus.psu" in capsys.readouterr().err

    def test_version_and_help(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            run("--version")
        assert excinfo.value.code == 0
        from mymc import __version__

        assert __version__ in capsys.readouterr().out
