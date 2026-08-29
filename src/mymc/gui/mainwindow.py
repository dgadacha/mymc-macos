"""The main window of the mymc graphical interface."""

import io
import os
import traceback

from PySide6.QtCore import QEventLoop, QSettings, QSize, Qt, QThread, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFormLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__, ps2icon, ps2mc, ps2save, render
from ..ps2mc_dir import (
    DF_PROTECTED,
    DF_WRITE,
    PS2MC_DIRENT_LENGTH,
    mode_is_dir,
)
from .appicon import app_icon, toolbar_icons
from .iconview import IconView

SAVE_FILTER = (
    "PS2 save files (*.psu *.max *.cbs *.sps *.xps);;"
    "EMS save file (*.psu);;"
    "MAX Drive save file (*.max);;"
    "All files (*)"
)
IMAGE_FILTER = (
    "PS2 memory card images (*.ps2 *.mcd *.mc2 *.bin *.mcr);;All files (*)"
)


def classify_file(path):
    """Say whether a file is a memory card image or a save file.

    Returns ``"card"``, ``"save"`` or ``None``.  This reads the file's
    header rather than trusting its name: memory card images turn up as
    .ps2, .mcd, .mc2, .mcr, .bin and with no extension at all, and .bin
    could just as easily be something else entirely.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(PS2MC_DIRENT_LENGTH * 3)
    except OSError:
        return None
    if head.startswith(ps2mc.PS2MC_MAGIC):
        return "card"
    if ps2save.detect_file_type(io.BytesIO(head)) is not None:
        return "save"
    return None


class _Worker(QThread):
    """Runs one callable off the UI thread, reporting progress."""

    progressed = Signal(int)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.finished_ok.emit(self._fn(self.progressed.emit))
        except Exception as why:  # surfaced in a dialog by the caller
            traceback.print_exc()
            self.failed.emit(str(why) or why.__class__.__name__)


def run_with_progress(parent, label, fn):
    """Run ``fn(progress_cb)`` in a thread behind a modal progress dialog.

    Returns the function's result, or raises whatever it raised.
    """
    dialog = QProgressDialog(label, None, 0, 100, parent)
    dialog.setWindowModality(Qt.WindowModal)
    dialog.setMinimumDuration(400)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)

    state = {}
    loop = QEventLoop()
    worker = _Worker(fn)
    worker.progressed.connect(dialog.setValue)
    worker.finished_ok.connect(lambda r: state.update(result=r))
    worker.failed.connect(lambda m: state.update(error=m))
    worker.finished.connect(loop.quit)
    worker.start()
    loop.exec()
    worker.wait()
    dialog.close()

    if "error" in state:
        raise RuntimeError(state["error"])
    return state.get("result")


class MainWindow(QMainWindow):
    def __init__(self, filename=None):
        super().__init__()
        self.setWindowTitle("mymc")
        self.setWindowIcon(app_icon())
        self.resize(940, 560)
        self.setAcceptDrops(True)

        self.settings = QSettings("mymc", "mymc")
        self.mc = None
        self.mc_file = None
        self.mc_path = None
        self.entries = []

        self._build_ui()
        self._build_actions()
        self._update_actions()

        if filename:
            self.open_image(filename)

    #
    # Construction
    #

    def _build_ui(self):
        splitter = QSplitter(Qt.Horizontal)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Save", "Size", "Modified", "Description"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        splitter.addWidget(self.table)

        side = QWidget()
        layout = QVBoxLayout(side)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.icon_view = IconView()
        layout.addWidget(self.icon_view, 1)

        self.title_label = QLabel("")
        self.title_label.setWordWrap(True)
        self.title_label.setAlignment(Qt.AlignCenter)
        font = self.title_label.font()
        font.setBold(True)
        self.title_label.setFont(font)
        layout.addWidget(self.title_label)

        self.details = QFormLayout()
        self.details.setLabelAlignment(Qt.AlignRight)
        self.detail_fields = {}
        for key, label in (
            ("dirname", "Directory"),
            ("size", "Size"),
            ("files", "Files"),
            ("modified", "Modified"),
            ("protection", "Protection"),
        ):
            value = QLabel("-")
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.detail_fields[key] = value
            self.details.addRow(label + ":", value)
        layout.addLayout(self.details)

        side.setMinimumWidth(260)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)

        self.free_bar = QProgressBar()
        self.free_bar.setMaximumWidth(160)
        self.free_bar.setTextVisible(False)
        self.free_label = QLabel("No memory card open")
        self.statusBar().addPermanentWidget(self.free_label)
        self.statusBar().addPermanentWidget(self.free_bar)

    def _build_actions(self):
        icons = toolbar_icons(self.palette().windowText().color())

        self.act_open = QAction(icons["open"], "Open…", self)
        self.act_open.setShortcut(QKeySequence.Open)
        self.act_open.triggered.connect(self.choose_image)

        self.act_close = QAction("Close", self)
        self.act_close.setShortcut(QKeySequence.Close)
        self.act_close.triggered.connect(self.close_image)

        self.act_import = QAction(icons["import"], "Import…", self)
        self.act_import.setShortcut("Ctrl+I")
        self.act_import.setToolTip("Import save files onto the memory card")
        self.act_import.triggered.connect(self.choose_import)

        self.act_export = QAction(icons["export"], "Export…", self)
        self.act_export.setShortcut("Ctrl+E")
        self.act_export.setToolTip("Export the selected saves to files")
        self.act_export.triggered.connect(self.export_selected)

        self.act_delete = QAction(icons["delete"], "Delete", self)
        self.act_delete.setShortcut(QKeySequence.Delete)
        self.act_delete.setToolTip("Delete the selected saves from the card")
        self.act_delete.triggered.connect(self.delete_selected)

        self.act_format = QAction("New Memory Card…", self)
        self.act_format.setShortcut(QKeySequence.New)
        self.act_format.triggered.connect(self.format_image)

        self.act_check = QAction("Check File System", self)
        self.act_check.triggered.connect(self.check_image)

        self.act_ascii = QAction("Transliterate Japanese Titles", self)
        self.act_ascii.setCheckable(True)
        self.act_ascii.setChecked(
            self.settings.value("ascii", False, type=bool)
        )
        self.act_ascii.triggered.connect(self._toggle_ascii)

        self.act_about = QAction("About mymc", self)
        self.act_about.setMenuRole(QAction.AboutRole)
        self.act_about.triggered.connect(self.about)

        toolbar = self.addToolBar("Main")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(22, 22))
        toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        toolbar.addAction(self.act_open)
        toolbar.addSeparator()
        toolbar.addAction(self.act_import)
        toolbar.addAction(self.act_export)
        toolbar.addAction(self.act_delete)

        menu = self.menuBar().addMenu("&File")
        menu.addAction(self.act_format)
        menu.addAction(self.act_open)
        menu.addAction(self.act_close)
        menu.addSeparator()
        menu.addAction(self.act_import)
        menu.addAction(self.act_export)
        menu.addSeparator()
        menu.addAction(self.act_delete)

        menu = self.menuBar().addMenu("&Card")
        menu.addAction(self.act_check)

        menu = self.menuBar().addMenu("&View")
        menu.addAction(self.act_ascii)

        menu = self.menuBar().addMenu("&Help")
        menu.addAction(self.act_about)

    #
    # Memory card handling
    #

    def _error(self, message, title="mymc"):
        QMessageBox.critical(self, title, message)

    def _mc_error(self, why, filename=None):
        message = getattr(why, "strerror", None) or str(why)
        name = getattr(why, "filename", None) or filename
        self._error((name + ": " + message) if name else message)

    def choose_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Memory Card Image",
            self.settings.value("memcard_dir", "", type=str),
            IMAGE_FILTER,
        )
        if path:
            self.open_image(path)

    def open_image(self, path):
        self.close_image()
        try:
            f = open(path, "r+b")
        except OSError as why:
            self._mc_error(why, path)
            return
        try:
            mc = ps2mc.ps2mc(f)
        except (ps2mc.error, OSError) as why:
            f.close()
            self._mc_error(why, path)
            return

        self.mc = mc
        self.mc_file = f
        self.mc_path = path
        directory = os.path.dirname(os.path.abspath(path))
        self.settings.setValue("memcard_dir", directory)
        self.setWindowTitle("mymc - " + os.path.basename(path))
        self.setWindowFilePath(path)
        self.refresh()

    def close_image(self):
        if self.mc is not None:
            try:
                self.mc.close()
            except Exception:
                traceback.print_exc()
            self.mc = None
        if self.mc_file is not None:
            self.mc_file.close()
            self.mc_file = None
        self.mc_path = None
        self.entries = []
        self.setWindowTitle("mymc")
        self.setWindowFilePath("")
        self._fill_table()
        self._update_status()
        self._update_actions()

    def refresh(self):
        self.entries = []
        if self.mc is not None:
            encoding = "ascii" if self.act_ascii.isChecked() else None
            try:
                d = self.mc.dir_open("/")
                try:
                    for ent in d:
                        if not mode_is_dir(ent[0]) or ent[8] in (".", ".."):
                            continue
                        dirname = "/" + ent[8]
                        raw = self.mc.get_icon_sys(dirname)
                        icon_sys = (
                            ps2save.unpack_icon_sys(raw) if raw is not None else None
                        )
                        title = (
                            ps2save.icon_sys_title(icon_sys, encoding)
                            if icon_sys
                            else ("Corrupt", "")
                        )
                        self.entries.append(
                            {
                                "ent": ent,
                                "name": ent[8],
                                "icon_sys": icon_sys,
                                "size": self.mc.dir_size(dirname),
                                "title": title,
                            }
                        )
                finally:
                    d.close()
            except (ps2mc.error, OSError) as why:
                self._mc_error(why, self.mc_path)

        self._fill_table()
        self._update_status()
        self._update_actions()

    def _fill_table(self):
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(len(self.entries))
        for row, info in enumerate(self.entries):
            ent = info["ent"]
            name_item = QTableWidgetItem(info["name"])
            name_item.setData(Qt.UserRole, row)

            size_item = QTableWidgetItem()
            size_item.setData(Qt.DisplayRole, "%d KB" % (info["size"] // 1024))
            size_item.setData(Qt.UserRole, info["size"])
            size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            m = ent[6]
            modified_item = QTableWidgetItem(
                "%04d-%02d-%02d %02d:%02d" % (m[5], m[4], m[3], m[2], m[1])
            )

            desc_item = QTableWidgetItem(ps2save.single_title(info["title"]))

            for col, item in enumerate(
                (name_item, size_item, modified_item, desc_item)
            ):
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)
        self._selection_changed()

    def _update_status(self):
        if self.mc is None:
            self.free_label.setText("No memory card open")
            self.free_bar.setRange(0, 1)
            self.free_bar.setValue(0)
            self.free_bar.setVisible(False)
            return
        free = self.mc.get_free_space()
        total = self.mc.get_allocatable_space()
        used = max(0, total - free)
        self.free_bar.setVisible(True)
        self.free_bar.setRange(0, max(1, total // 1024))
        self.free_bar.setValue(used // 1024)
        self.free_bar.setToolTip(
            "%d KB used of %d KB" % (used // 1024, total // 1024)
        )
        self.free_label.setText(
            "{:,} KB free of {:,} KB".format(free // 1024, total // 1024)
        )

    def _update_actions(self):
        has_card = self.mc is not None
        has_selection = bool(self._selected_rows())
        self.act_close.setEnabled(has_card)
        self.act_import.setEnabled(has_card)
        self.act_check.setEnabled(has_card)
        self.act_export.setEnabled(has_card and has_selection)
        self.act_delete.setEnabled(has_card and has_selection)

    def _selected_rows(self):
        rows = []
        for index in self.table.selectionModel().selectedRows() if self.table.selectionModel() else []:
            item = self.table.item(index.row(), 0)
            if item is not None:
                rows.append(item.data(Qt.UserRole))
        return rows

    def _selection_changed(self):
        rows = self._selected_rows()
        self._update_actions()
        if len(rows) != 1:
            self.icon_view.set_icon(
                None,
                placeholder=(
                    "Select a save to preview its icon"
                    if self.mc is not None
                    else "Drop a memory card image here,\nor use File \u25b8 Open"
                ),
            )
            self.title_label.setText(
                "%d saves selected" % len(rows) if rows else ""
            )
            for field in self.detail_fields.values():
                field.setText("-")
            return

        info = self.entries[rows[0]]
        ent = info["ent"]
        self.title_label.setText(ps2save.single_title(info["title"]))
        m = ent[6]
        protection = ent[0] & (DF_PROTECTED | DF_WRITE)
        protection = {
            0: "Delete protected",
            DF_WRITE: "Not protected",
            DF_PROTECTED: "Copy & delete protected",
        }.get(protection, "Copy protected")
        self.detail_fields["dirname"].setText(info["name"])
        self.detail_fields["size"].setText("{:,} KB".format(info["size"] // 1024))
        self.detail_fields["files"].setText(str(max(0, ent[2] - 2)))
        self.detail_fields["modified"].setText(
            "%04d-%02d-%02d %02d:%02d:%02d" % (m[5], m[4], m[3], m[2], m[1], m[0])
        )
        self.detail_fields["protection"].setText(protection)

        self._show_icon(info)

    def _show_icon(self, info):
        icon_sys = info["icon_sys"]
        if icon_sys is None:
            self.icon_view.set_icon(None, placeholder="This save has no icon.sys")
            return
        name = icon_sys[15] or "list.icn"
        try:
            f = self.mc.open("/" + info["name"] + "/" + name, "rb")
            try:
                data = f.read()
            finally:
                f.close()
            icon = ps2icon.parse_icon(data)
        except (ps2mc.error, ps2icon.IconError, OSError, ValueError) as why:
            self.icon_view.set_icon(None, placeholder="Icon unavailable\n(%s)" % why)
            return
        self.icon_view.set_icon(
            icon, lighting=render.lighting_from_icon_sys(icon_sys)
        )

    #
    # Commands
    #

    def _toggle_ascii(self, checked):
        self.settings.setValue("ascii", bool(checked))
        self.refresh()

    def choose_import(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Import Save Files",
            self.settings.value("savefile_dir", "", type=str),
            SAVE_FILTER,
        )
        if paths:
            self.import_files(paths)

    def import_files(self, paths):
        if self.mc is None:
            return
        imported = 0
        skipped = []
        for path in paths:
            try:
                with open(path, "rb") as f:
                    sf = ps2save.load_save_file(f)
                name = sf.get_directory()[8]
                sf = run_with_progress(
                    self,
                    "Reading %s…" % os.path.basename(path),
                    lambda report, sf=sf: (sf.decompress(report), sf)[1],
                )
                if self.mc.import_save_file(sf, True):
                    imported += 1
                else:
                    skipped.append(os.path.basename(path) + " (already present)")
            except (ps2save.error, ps2mc.error, OSError, RuntimeError) as why:
                skipped.append("%s (%s)" % (os.path.basename(path), why))

        if paths:
            directory = os.path.dirname(os.path.abspath(paths[-1]))
            self.settings.setValue("savefile_dir", directory)
        self.refresh()

        if skipped:
            QMessageBox.warning(
                self,
                "Import",
                "Imported %d save%s.\n\nNot imported:\n  %s"
                % (imported, "" if imported == 1 else "s", "\n  ".join(skipped)),
            )
        elif imported:
            self.statusBar().showMessage(
                "Imported %d save%s." % (imported, "" if imported == 1 else "s"),
                4000,
            )

    def export_selected(self):
        rows = self._selected_rows()
        if self.mc is None or not rows:
            return

        saves = []
        for row in rows:
            name = self.entries[row]["name"]
            try:
                sf = self.mc.export_save_file("/" + name)
            except (ps2mc.error, OSError) as why:
                self._mc_error(why, name)
                continue
            saves.append((name, sf, ps2save.make_longname(name, sf)))
        if not saves:
            return

        directory = self.settings.value("savefile_dir", "", type=str)

        if len(saves) == 1:
            name, sf, longname = saves[0]
            path, selected = QFileDialog.getSaveFileName(
                self,
                "Export " + name,
                os.path.join(directory, longname + ".psu"),
                "EMS save file (*.psu);;MAX Drive save file (*.max)",
            )
            if not path:
                return
            as_max = path.lower().endswith(".max") or "MAX" in selected
            if not os.path.splitext(path)[1]:
                path += ".max" if as_max else ".psu"
            try:
                self._write_save(sf, path, as_max)
            except (ps2save.error, OSError, RuntimeError) as why:
                self._error("%s: %s" % (os.path.basename(path), why))
                return
            self.settings.setValue(
                "savefile_dir", os.path.dirname(os.path.abspath(path))
            )
            self.statusBar().showMessage("Exported " + os.path.basename(path), 4000)
            return

        target = QFileDialog.getExistingDirectory(
            self, "Export Save Files", directory
        )
        if not target:
            return
        exported = 0
        for name, sf, longname in saves:
            path = os.path.join(target, longname + ".psu")
            try:
                self._write_save(sf, path, False)
                exported += 1
            except (ps2save.error, OSError, RuntimeError) as why:
                self._error("%s: %s" % (os.path.basename(path), why))
        self.settings.setValue("savefile_dir", target)
        self.statusBar().showMessage(
            "Exported %d save%s." % (exported, "" if exported == 1 else "s"), 4000
        )

    def _write_save(self, sf, path, as_max):
        if not as_max:
            with open(path, "wb") as f:
                sf.save_ems(f)
            return

        def work(report):
            with open(path, "wb") as f:
                sf.save_max_drive(f, progress=report)

        run_with_progress(
            self, "Compressing %s…" % os.path.basename(path), work
        )

    def delete_selected(self):
        rows = self._selected_rows()
        if self.mc is None or not rows:
            return
        names = [self.entries[row]["name"] for row in rows]
        if len(names) == 1:
            what = "%s (%s)" % (
                names[0],
                ps2save.single_title(self.entries[rows[0]]["title"]),
            )
        else:
            what = "%d saves" % len(names)

        answer = QMessageBox.question(
            self,
            "Delete Save",
            "Permanently delete %s from the memory card?" % what,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        for name in names:
            try:
                self.mc.rmdir("/" + name)
            except (ps2mc.error, OSError) as why:
                self._mc_error(why, name)
        self.refresh()

    def format_image(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "New Memory Card Image",
            os.path.join(
                self.settings.value("memcard_dir", "", type=str), "Mcd001.ps2"
            ),
            IMAGE_FILTER,
        )
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".ps2"
        params = (
            True,
            ps2mc.PS2MC_STANDARD_PAGE_SIZE,
            ps2mc.PS2MC_STANDARD_PAGES_PER_ERASE_BLOCK,
            ps2mc.PS2MC_STANDARD_PAGES_PER_CARD,
        )
        try:
            with open(path, "w+b") as f:
                ps2mc.ps2mc(f, True, params).close()
        except (ps2mc.error, OSError) as why:
            self._mc_error(why, path)
            return
        self.open_image(path)

    def check_image(self):
        if self.mc is None:
            return
        problems = []
        try:
            ok = self.mc.check(log=problems.append)
        except (ps2mc.error, OSError) as why:
            self._mc_error(why, self.mc_path)
            return
        if ok:
            QMessageBox.information(
                self, "Check File System", "No errors found."
            )
        else:
            QMessageBox.warning(
                self,
                "Check File System",
                "Problems found:\n\n" + "\n".join(problems[:40]),
            )

    def about(self):
        QMessageBox.about(
            self,
            "About mymc",
            "<h3>mymc %s</h3>"
            "<p>A utility for manipulating PlayStation&nbsp;2 memory card "
            "images.</p>"
            "<p>Originally written by Ross Ridge and released into the public "
            "domain. This is a Python&nbsp;3 port with a Qt interface that "
            "runs natively on macOS.</p>"
            "<p>Do not modify a memory card image while PCSX2 has it open.</p>"
            % __version__,
        )

    #
    # Drag and drop
    #

    def _droppable(self, event):
        """Classify the dragged files: [(kind, path), ...]."""
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        dropped = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            kind = classify_file(path)
            if kind is not None:
                dropped.append((kind, path))
        return dropped

    def dragEnterEvent(self, event):
        # Classify once here: dragMoveEvent fires on every mouse move and
        # must not re-read the files each time.
        self._drop_targets = self._droppable(event)
        if self._drop_targets:
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if getattr(self, "_drop_targets", None):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._drop_targets = []

    def dropEvent(self, event):
        dropped = getattr(self, "_drop_targets", None) or self._droppable(event)
        self._drop_targets = []
        if not dropped:
            return
        event.acceptProposedAction()
        self.open_dropped(dropped)

    def open_dropped(self, dropped):
        """Act on dropped files: open a card, import saves into it."""
        cards = [path for (kind, path) in dropped if kind == "card"]
        saves = [path for (kind, path) in dropped if kind == "save"]

        if cards:
            self.open_image(cards[0])
            if len(cards) > 1:
                self.statusBar().showMessage(
                    "Opened %s; drop the others one at a time."
                    % os.path.basename(cards[0]),
                    5000,
                )
        if saves:
            if self.mc is None:
                QMessageBox.information(
                    self,
                    "Import",
                    "Open a memory card image first, then drop save files "
                    "onto it to import them.",
                )
                return
            self.import_files(saves)

    def closeEvent(self, event):
        self.close_image()
        super().closeEvent(event)
