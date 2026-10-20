# -*- coding: utf-8 -*-
"""Datasmith batch page, sharing the host lock and editor tick lifecycle."""
from __future__ import annotations

import importlib
import os
import traceback

import unreal
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QDialog, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from . import datasmith_import_processor as processor, version

if processor.API_VERSION != version.DATASMITH_IMPORT_API_VERSION:
    processor = importlib.reload(processor)

API_VERSION = "1.2.0"


class DatasmithImportPage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.files, self.items = [], []
        self.session, self.tick_handle, self.settings = None, None, None
        self.busy = self.preparing = self._restoring = False
        self.last_folder = ""
        self.rename_dialog = None
        self.rename_text = None
        self._renames = {}
        self._report_failed = False
        self._last_warning = ""
        self.mode = 'normal'
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        self.intro = QLabel()
        self.intro.setWordWrap(True)
        layout.addWidget(self.intro)
        folder = QHBoxLayout()
        label = QLabel("UE 输出根目录")
        self.output = QLineEdit(processor.DEFAULT_OUTPUT)
        label.setBuddy(self.output)
        self.output.setAccessibleName("UE 输出根目录")
        self.output.setToolTip("例如 /Game/DatasmithImports，每个文件建立同名子文件夹。")
        folder.addWidget(label)
        folder.addWidget(self.output, 1)
        pick = QPushButton("读取选中文件夹")
        pick.clicked.connect(self._pick_folder)
        folder.addWidget(pick)
        layout.addLayout(folder)
        note = QLabel("当前关卡未指定保存路径时，将保存到输出根目录下的“批量导入场景”；重名自动追加 _1、_2。每次全部保存也会保存其他已修改资源。")
        note.setWordWrap(True)
        font = note.font()
        font.setPointSize(max(9, font.pointSize() - 1))
        note.setFont(font)
        layout.addWidget(note)
        self.memory_label = QLabel("内存监测：可用物理内存低于 2 GB 或虚拟内存低于 4 GB 时提示预警，仍继续导入。")
        self.memory_label.setWordWrap(True)
        layout.addWidget(self.memory_label)
        actions = QHBoxLayout()
        for text, callback in (("添加 Datasmith 文件…", self.add_files), ("移除选中", self.remove_selected),
                               ("清空队列", self.clear_files), ("上移", lambda: self.move_selected(-1)),
                               ("下移", lambda: self.move_selected(1))):
            button = QPushButton(text)
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch(1)
        mode_box = QVBoxLayout()
        mode_box.setSpacing(3)
        modes = QHBoxLayout()
        modes.setSpacing(12)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons = {}
        for key, title in processor.MODES.items():
            button = QPushButton(title)
            button.setCheckable(True)
            button.setFixedSize(140, 30)
            button.setAccessibleName(title)
            button.setStyleSheet('QPushButton:checked { background-color: #315f8c; color: white; border: 1px solid #24496c; border-radius: 3px; font-weight: bold; }')
            button.setToolTip('按现有流程导入资产。' if key == 'normal' else '一次开始，自动分阶段导入几何体、材质和贴图，并恢复校验材质绑定。')
            button.clicked.connect(lambda checked=False, selected=key: self.set_mode(selected))
            self.mode_group.addButton(button)
            self.mode_buttons[key] = button
            modes.addWidget(button)
        mode_box.addLayout(modes)
        self.mode_label = QLabel()
        self.mode_label.setAlignment(Qt.AlignCenter)
        self.mode_label.setFont(font)
        mode_box.addWidget(self.mode_label)
        actions.addLayout(mode_box)
        actions.addStretch(1)
        self.start_button = QPushButton("导入设置并开始")
        self.start_button.clicked.connect(self.start)
        actions.addWidget(self.start_button)
        layout.addLayout(actions)
        self.summary = QLabel("请手动选择 .udatasmith 文件；可上移、下移调整导入顺序。")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(("顺序", "源文件", "导入名称", "UE 目标目录", "状态", "说明"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for col, width in enumerate((50, 220, 155, 260, 140, 260)):
            self.table.setColumnWidth(col, width)
        layout.addWidget(self.table, 1)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        self.log.document().setMaximumBlockCount(3000)
        layout.addWidget(self.log)
        self.output.editingFinished.connect(self._changed)
        self.set_mode('normal', persist=False)

    def set_mode(self, mode, persist=True):
        if self.session or self.preparing or self.host._scan_active:
            mode = self.mode
        self.mode = mode if mode in processor.MODES else 'normal'
        for key, button in self.mode_buttons.items():
            button.setChecked(key == self.mode)
        self.mode_label.setText('当前模式：' + processor.MODES[self.mode])
        self.intro.setText('仅导入资产，不在关卡摆放模型。' + (
            '大文件模式：分批导入几何体和贴图，每批保存与清理 → 材质 → 保存与清理 → 自动恢复并校验材质绑定 → 全部保存。只需开始一次。'
            if self.mode == 'large' else '普通模式：导入 → 等待编译 → 全部保存 → 清理临时资源 → 等待五秒 → 下一个。'))
        if persist:
            self._changed()

    def save_state(self):
        return {"output": self.output.text(), "last_folder": self.last_folder, "mode": self.mode}

    def restore_state(self, state):
        self._restoring = True
        try:
            self.output.setText(str(state.get("output", processor.DEFAULT_OUTPUT)))
            self.last_folder = str(state.get("last_folder", ""))
            self.set_mode(state.get('mode', 'normal'), persist=False)
        finally:
            self._restoring = False

    def _changed(self):
        if not self._restoring:
            self.host._save_state()

    def _message(self, value, error=False):
        self.log.append(str(value))
        (unreal.log_error if error else unreal.log)("[DatasmithBatchImport] " + str(value))

    def _error(self, title, exc):
        self._message(str(exc), True)
        QMessageBox.warning(self, title, str(exc))

    def _pick_folder(self):
        try:
            paths = list(unreal.EditorUtilityLibrary.get_selected_folder_paths())
            if not paths:
                paths = list(unreal.EditorUtilityLibrary.get_selected_path_view_folder_paths())
            if len(paths) != 1:
                raise ValueError("请在内容浏览器中只选择一个文件夹。")
            self.output.setText(processor.game_directory(paths[0]))
            self._changed()
        except Exception as exc:
            self._error("读取文件夹", exc)

    def add_files(self):
        if self.session or self.host._scan_active or self.preparing:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "选择 Datasmith 文件", self.last_folder, "Datasmith 文件 (*.udatasmith)")
        if paths:
            self.files.extend(paths)
            self.last_folder = os.path.dirname(paths[-1])
            self.items = []
            self._populate()
            self._changed()

    def remove_selected(self):
        if self.session or self.preparing:
            return
        for row in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            del self.files[row]
        self.items = []
        self._populate()

    def clear_files(self):
        if not self.session and not self.preparing:
            self.files, self.items = [], []
            self._populate()

    def move_selected(self, direction):
        if self.session or self.preparing:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if len(rows) != 1:
            return
        row, target = rows[0], rows[0] + direction
        if 0 <= target < len(self.files):
            self.files[row], self.files[target] = self.files[target], self.files[row]
            self.items = []
            self._populate()
            self.table.selectRow(target)

    def start(self):
        if self.session or self.preparing or self.host._scan_active:
            return
        locked = False
        try:
            if not self.files:
                raise ValueError("请先选择需要导入的 .udatasmith 文件。")
            root = processor.game_directory(self.output.text())
            backend = processor.UnrealBackend()
            self.items = processor.build_queue(self.files, root, backend.occupied)
            self._populate()
            if not any(item.status == "waiting" for item in self.items):
                raise ValueError("队列中没有有效的 .udatasmith 文件。")
            self.preparing, locked = True, True
            self.host._begin_scan("Datasmith 批量导入")
            if self.settings is None:
                self.settings = unreal.DatasmithImportOptions()
            accepted = self._choose_options()
            if not accepted or self.host._closing or self.host._scan_cancel_requested:
                self._message("已取消导入设置，未执行导入。")
                return
            self._renames = {}
            if self.rename_dialog is not None:
                self.rename_dialog.hide()
            self._report_failed = False
            self._last_warning = ""
            if hasattr(backend, 'validate_mode'):
                backend.validate_mode(self.mode, self.settings)
            session = processor.BatchSession(self.items, root, self.settings, backend, mode=self.mode)
            # Refuse to mutate the project if the initial journal cannot be written.
            report = session.write_report()
            self.host._save_state()
            self.session = session
            session.journal = self._before_import
            self.tick_handle = unreal.register_slate_post_tick_callback(self._tick)
            self._message("当前模式：{}；批次报告：{}".format(processor.MODES[self.mode], report))
            self._show_renames()
            locked = False
        except Exception as exc:
            self._error("启动批量导入失败", exc)
            if self.session:
                self.stop(cancelled=True)
                locked = False
        finally:
            self.preparing = False
            if locked:
                self.host._end_scan()

    def _choose_options(self):
        return unreal.DatasmithBatchImportLibrary.show_options(self.settings)

    def _before_import(self):
        self.session.write_report()
        self._memory_display()
        self.host._scan_progress(self.session.index, len(self.items))
        if self.host._closing or self.host._scan_cancel_requested:
            self.session.stop_requested = True

    def _show_renames(self):
        if self.host._closing:
            return
        changed = False
        for row, item in enumerate(self.items):
            if item.name != item.original_name and self._renames.get(row) != item.target:
                self._renames[row] = item.target
                changed = True
                self._message("名称调整：{} → {}".format(item.source, item.target))
        if not changed:
            return
        if self.rename_dialog is None:
            self.rename_dialog = QDialog(self.window())
            self.rename_dialog.setWindowTitle("Datasmith 名称调整提示")
            self.rename_dialog.resize(660, 300)
            box = QVBoxLayout(self.rename_dialog)
            box.addWidget(QLabel("以下文件按队列顺序分配名称，导入会自动继续："))
            self.rename_text = QTextEdit()
            self.rename_text.setReadOnly(True)
            box.addWidget(self.rename_text)
            close = QPushButton("关闭提示")
            close.clicked.connect(self.rename_dialog.hide)
            box.addWidget(close)
        self.rename_text.setPlainText("\n\n".join("{}\n→ {}".format(self.items[row].source, target) for row, target in sorted(self._renames.items())))
        self.rename_dialog.show()

    def _write_report(self):
        try:
            self.session.write_report()
        except Exception as exc:
            self._report_failed = True
            self._message("报告写入失败：" + str(exc), True)

    def _memory_display(self):
        session = self.session
        if not session:
            return
        if session.warning:
            self.memory_label.setText(session.warning)
            self.memory_label.setStyleSheet("color: #b45309; font-weight: bold;")
            if session.warning != self._last_warning:
                self._message(session.warning)
            self._last_warning = session.warning
        elif session.memory:
            self.memory_label.setStyleSheet("")
            self.memory_label.setText("内存监测：可用物理内存 {:.2f} GB，可用虚拟内存 {:.2f} GB。".format(
                session.memory['available_physical_gb'], session.memory['available_virtual_gb']))
            self._last_warning = ""

    def _tick(self, _delta=0.0):
        if self.busy or not self.session:
            return
        self.busy = True
        try:
            session = self.session
            row = session.index
            phase = session.phase
            if phase == "cooldown" and session.remaining_seconds() > 0:
                self.summary.setText("保存和清理完成；等待 {:.1f} 秒后自动继续。".format(session.remaining_seconds()))
                if self.host._scan_cancel_requested or self.host._closing:
                    self.stop(cancelled=True)
                return
            labels = {"import": "导入几何体" if session.mode == 'large' else "导入资产", "import_textures": "分批导入贴图", "import_materials": "导入材质",
                      "validate": "逐网格恢复并校验材质", "compile": "等待资源编译", "save": "全部保存", "cleanup": "清理临时资源"}
            if row < len(self.items):
                item = self.items[row]
                label = labels.get(phase, "检查下一文件")
                self._message("{}：{}".format(label, item.source))
                if item.status != "invalid":
                    self.table.setItem(row, 4, QTableWidgetItem(label))
                    self.summary.setText("{} / {}：{} — {}".format(row + 1, len(self.items),
                        label, os.path.basename(item.source)))
                    # Flush progress before the synchronous operation; the busy guard prevents reentry.
                    self.host._scan_progress(session.index, len(self.items))
            session.step(cancel=self.host._scan_cancel_requested or self.host._closing)
            self._write_report()
            self._populate()
            self._memory_display()
            self._show_renames()
            if row < len(self.items) and phase == "cleanup":
                item = self.items[row]
                self._message("{}：{}；{}".format(processor.STATUS[item.status], item.target, item.message), item.status != "completed")
            self.host._scan_progress(session.index, len(self.items))
        except Exception:
            self._message(traceback.format_exc(), True)
            self.session.stop_requested = True
        finally:
            self.busy = False
            if self.session:
                if self.host._scan_cancel_requested or self.host._closing:
                    self.stop(cancelled=True)
                elif self.session.done:
                    self.stop()

    def stop(self, cancelled=False):
        if not self.session:
            return
        self.session.stop_requested = self.session.stop_requested or cancelled
        if self.busy:
            return
        self.busy = True
        try:
            # A close/cancel arriving between import and save must not bypass Save All.
            while self.session.phase in ("compile", "save", "cleanup", "validate"):
                self.session.step(cancel=cancelled)
            if not self.session.done:
                self.session.step(cancel=True)
            self._write_report()
            self._populate()
            self._message("批次{}。{}".format("已取消" if self.session.cancelled else "完成",
                "报告曾写入失败，请同时保留本页日志。" if self._report_failed else "逐文件结果见表格与批次报告。"))
        finally:
            handle, self.tick_handle = self.tick_handle, None
            self.session = None
            try:
                if handle is not None:
                    unreal.unregister_slate_post_tick_callback(handle)
            finally:
                self.busy = False
                self.host._end_scan()

    def _populate(self):
        self.table.setRowCount(len(self.files))
        for row, source in enumerate(self.files):
            item = self.items[row] if row < len(self.items) else None
            values = (str(row + 1), os.path.basename(source), item.name if item else "开始时分配",
                      item.target if item else "", processor.STATUS[item.status] if item else "等待开始",
                      item.message if item else "")
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(source if col == 1 else value)
                self.table.setItem(row, col, cell)
        if self.items:
            counts = {status: sum(i.status == status for i in self.items) for status in processor.STATUS}
            self.summary.setText("文件 {}；完成 {}；未完整导入 {}；导入失败 {}；保存失败 {}；资源处理失败 {}；无效 {}；取消 {}。低内存仅预警，仍继续。".format(
                len(self.files), counts['completed'], counts['incomplete'], counts['import_failed'], counts['save_failed'], counts['maintenance_failed'], counts['invalid'], counts['cancelled']))
        else:
            self.summary.setText("已选择 {} 个文件，按表格从上到下导入。".format(len(self.files)))
