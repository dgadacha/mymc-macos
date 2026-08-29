"""Tests for the Qt interface, driven headlessly.

These run against the offscreen platform plugin, so they need no display.
Modal dialogs are replaced by recorders: a real QMessageBox would block
forever with nobody to dismiss it.
"""

import io
import os

import pytest

from conftest import STANDARD_PARAMS, make_save
from mymc import ps2mc

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (  # noqa: E402
    QMimeData,
    QPoint,
    QPointF,
    QSettings,
    Qt,
    QUrl,
)
from PySide6.QtGui import QDragEnterEvent, QDropEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from mymc.gui import mainwindow as mw  # noqa: E402
from mymc.gui.mainwindow import MainWindow, classify_file  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def dialogs(monkeypatch):
    """Record modal dialogs instead of showing them."""
    seen = []

    def record(kind):
        def fake(*args, **kwargs):
            seen.append((kind, args[2] if len(args) > 2 else ""))
            return QMessageBox.Yes

        return staticmethod(fake)

    for name in ("warning", "information", "critical", "question", "about"):
        monkeypatch.setattr(QMessageBox, name, record(name))
    return seen


@pytest.fixture
def card(tmp_path):
    """A formatted card image on disk."""
    path = tmp_path / "Mcd001.ps2"
    with open(path, "w+b") as f:
        ps2mc.ps2mc(f, True, STANDARD_PARAMS).close()
    return str(path)


@pytest.fixture
def psu(tmp_path):
    sf, files = make_save("BASLUS-20678SAVE")
    path = tmp_path / "save.psu"
    with open(path, "wb") as f:
        sf.save_ems(f)
    return str(path)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Keep the tests out of the real preferences file."""
    path = str(tmp_path / "settings.ini")
    monkeypatch.setattr(
        mw, "QSettings", lambda *a, **k: QSettings(path, QSettings.IniFormat)
    )
    return path


@pytest.fixture
def window(qapp, dialogs, settings):
    w = MainWindow()
    yield w
    w.close_image()
    w.deleteLater()


def drop(window, paths):
    """Simulate a real drag and drop, returning whether it was accepted."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    enter = QDragEnterEvent(
        QPoint(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier
    )
    window.dragEnterEvent(enter)
    accepted = enter.isAccepted()
    window.dropEvent(
        QDropEvent(
            QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier
        )
    )
    return accepted


class TestClassifyFile:
    def test_recognises_a_card_image(self, card):
        assert classify_file(card) == "card"

    def test_recognises_a_card_image_without_ecc(self, tmp_path):
        path = tmp_path / "noecc.mcd"
        with open(path, "w+b") as f:
            ps2mc.ps2mc(f, True, (False, 512, 16, 16384)).close()
        assert classify_file(str(path)) == "card"

    def test_ignores_the_extension(self, card, tmp_path):
        # a card image is recognised whatever it is called
        renamed = tmp_path / "memcard_no_extension"
        os.rename(card, renamed)
        assert classify_file(str(renamed)) == "card"

    def test_recognises_save_files(self, psu):
        assert classify_file(psu) == "save"

    def test_rejects_other_files(self, tmp_path):
        path = tmp_path / "notes.txt"
        path.write_bytes(b"just some text" * 200)
        assert classify_file(str(path)) is None

    def test_missing_file(self, tmp_path):
        assert classify_file(str(tmp_path / "nope")) is None


class TestDragAndDrop:
    def test_card_image_opens_with_nothing_loaded(self, window, card):
        assert window.mc is None
        assert drop(window, [card])
        assert window.mc is not None
        assert os.path.basename(card) in window.windowTitle()

    def test_save_is_imported_onto_the_open_card(self, window, card, psu):
        drop(window, [card])
        assert window.table.rowCount() == 0
        assert drop(window, [psu])
        assert window.table.rowCount() == 1
        assert window.table.item(0, 0).text() == "BASLUS-20678SAVE"

    def test_card_and_save_dropped_together(self, window, card, psu):
        assert drop(window, [card, psu])
        assert window.mc is not None
        assert window.table.rowCount() == 1

    def test_dropping_a_save_with_no_card_explains_itself(
        self, window, psu, dialogs
    ):
        assert drop(window, [psu])
        assert window.mc is None
        assert any("Open a memory card image first" in str(m) for _, m in dialogs)

    def test_unrelated_file_is_refused(self, window, card, tmp_path):
        drop(window, [card])
        junk = tmp_path / "junk.bin"
        junk.write_bytes(b"\x00\x01\x02" * 600)
        assert not drop(window, [junk])

    def test_second_card_replaces_the_first(self, window, card, psu, tmp_path):
        drop(window, [card, psu])
        other = tmp_path / "Mcd002.ps2"
        with open(other, "w+b") as f:
            ps2mc.ps2mc(f, True, STANDARD_PARAMS).close()
        assert drop(window, [str(other)])
        assert "Mcd002.ps2" in window.windowTitle()
        assert window.table.rowCount() == 0

    def test_duplicate_import_is_reported(self, window, card, psu, dialogs):
        drop(window, [card])
        drop(window, [psu])
        dialogs.clear()
        drop(window, [psu])
        assert window.table.rowCount() == 1
        assert any("already present" in str(m) for _, m in dialogs)

    def test_card_survives_the_round_trip(self, window, card, psu):
        drop(window, [card, psu])
        assert window.mc.check(log=lambda m: None)


class TestWindow:
    def test_selecting_a_save_shows_its_details(self, window, card, psu):
        drop(window, [card, psu])
        window.table.selectRow(0)
        assert window.title_label.text() == "UNLIMITED SAGA SYSTEMDATA"
        assert window.detail_fields["dirname"].text() == "BASLUS-20678SAVE"
        assert window.detail_fields["protection"].text() == "Not protected"

    def test_icon_is_rendered(self, window, card, psu):
        pytest.importorskip("numpy")
        drop(window, [card, psu])
        window.table.selectRow(0)
        assert window.icon_view._icon is not None
        assert window.icon_view._icon.triangle_count == 12

    def test_export_writes_a_psu(self, window, card, psu, tmp_path):
        drop(window, [card, psu])
        out = tmp_path / "exported.psu"
        sf = window.mc.export_save_file("/BASLUS-20678SAVE")
        window._write_save(sf, str(out), as_max=False)
        assert classify_file(str(out)) == "save"

    def test_free_space_is_shown(self, window, card):
        drop(window, [card])
        assert "free of" in window.free_label.text()

    def test_delete_removes_the_save(self, window, card, psu, dialogs):
        drop(window, [card, psu])
        window.table.selectRow(0)
        window.delete_selected()  # the stubbed question answers Yes
        assert window.table.rowCount() == 0
        assert window.mc.check(log=lambda m: None)


class TestRecentFiles:
    def test_menu_starts_empty(self, window):
        assert window.recent_paths() == []
        assert not window.recent_menu.isEnabled()

    def test_opening_a_card_remembers_it(self, window, card):
        window.open_image(card)
        assert window.recent_paths() == [os.path.abspath(card)]
        assert window.recent_menu.isEnabled()
        # the entries, plus a separator and Clear Menu
        labels = [a.text() for a in window.recent_menu.actions() if a.text()]
        assert labels == [os.path.basename(card), "Clear Menu"]

    def test_a_dropped_card_is_remembered_too(self, window, card):
        drop(window, [card])
        assert window.recent_paths() == [os.path.abspath(card)]

    def test_most_recent_comes_first_without_duplicates(
        self, window, card, tmp_path
    ):
        other = tmp_path / "Mcd002.ps2"
        with open(other, "w+b") as f:
            ps2mc.ps2mc(f, True, STANDARD_PARAMS).close()
        window.open_image(card)
        window.open_image(str(other))
        window.open_image(card)
        assert window.recent_paths() == [
            os.path.abspath(card),
            os.path.abspath(str(other)),
        ]

    def test_the_list_is_capped(self, window, tmp_path):
        for i in range(mw.MAX_RECENT + 4):
            path = tmp_path / ("card%02d.ps2" % i)
            with open(path, "w+b") as f:
                ps2mc.ps2mc(f, True, STANDARD_PARAMS).close()
            window.open_image(str(path))
        assert len(window.recent_paths()) == mw.MAX_RECENT

    def test_deleted_cards_drop_out(self, window, card):
        window.open_image(card)
        window.close_image()
        os.remove(card)
        assert window.recent_paths() == []

    def test_a_card_that_failed_to_open_is_not_remembered(
        self, window, tmp_path
    ):
        junk = tmp_path / "junk.ps2"
        junk.write_bytes(b"not a card" * 200)
        window.open_image(str(junk))
        assert window.recent_paths() == []

    def test_clear_menu(self, window, card):
        window.open_image(card)
        window.clear_recent()
        assert window.recent_paths() == []
        assert not window.recent_menu.isEnabled()

    def test_a_menu_entry_opens_its_card(self, window, card, psu):
        drop(window, [card, psu])
        window.close_image()
        assert window.mc is None
        entry = window.recent_menu.actions()[0]
        entry.trigger()
        assert window.mc is not None
        assert window.table.rowCount() == 1

    def test_the_list_survives_a_new_window(self, window, card, qapp, dialogs):
        window.open_image(card)
        again = MainWindow()
        try:
            assert again.recent_paths() == [os.path.abspath(card)]
        finally:
            again.close_image()
            again.deleteLater()
