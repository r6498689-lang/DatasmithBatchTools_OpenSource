"""Small host window for the three released pages."""
from __future__ import annotations

import unreal
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QPushButton, QTabWidget, QVBoxLayout, QWidget

from . import datasmith_import_ui, material_instance_ui, storage, texture_ui

STATE_FILE = "window_state.json"
_window = None


class BatchToolsWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Datasmith Batch Tools")
        self.resize(1180, 760)
        self._scan_active = False
        self._scan_cancel_requested = False
        self._closing = False

        root = QWidget(self)
        layout = QVBoxLayout(root)
        self.tabs = QTabWidget(root)
        self.material_page = material_instance_ui.MaterialInstancePage(self)
        self.texture_page = texture_ui.TextureBatchPage(self)
        self.import_page = datasmith_import_ui.DatasmithImportPage(self)
        self.tabs.addTab(self.material_page, "批量生成材质实例")
        self.tabs.addTab(self.texture_page, "批量修改贴图属性")
        self.tabs.addTab(self.import_page, "批量导入 Datasmith")
        layout.addWidget(self.tabs, 1)

        footer = QWidget(root)
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        self.scan_progress_label = QLabel("就绪", footer)
        self.scan_progress_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        footer_layout.addWidget(self.scan_progress_label)
        self.cancel_button = QPushButton("取消当前任务", footer)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        footer_layout.addWidget(self.cancel_button)
        layout.addWidget(footer)
        self.setCentralWidget(root)

        state = storage.read_json(STATE_FILE, default={}) or {}
        for key, page in (("material", self.material_page), ("texture", self.texture_page), ("import", self.import_page)):
            restore = getattr(page, "restore_state", None)
            if restore and isinstance(state.get(key), dict):
                restore(state[key])
        self.tabs.setCurrentIndex(max(0, min(int(state.get("tab", 0)), self.tabs.count() - 1)))
        self.tabs.currentChanged.connect(self._save_state)

    def _save_state(self, *_args):
        try:
            storage.write_json(STATE_FILE, {
                "material": self.material_page.save_state(),
                "texture": self.texture_page.save_state() if hasattr(self.texture_page, "save_state") else {},
                "import": self.import_page.save_state(),
                "tab": self.tabs.currentIndex(),
            })
        except Exception as exc:
            unreal.log_warning("[DatasmithBatchTools] 状态保存失败：{}".format(exc))

    def _begin_scan(self, title):
        self._scan_active = True
        self._scan_cancel_requested = False
        self.cancel_button.setEnabled(True)
        self.scan_progress_label.setText(str(title) + "：准备中")

    def _scan_progress(self, done, total):
        self.scan_progress_label.setText("处理中：{} / {}".format(done, total))
        QApplication.processEvents()
        return not self._scan_cancel_requested and not self._closing

    def _end_scan(self):
        self._scan_active = False
        self.cancel_button.setEnabled(False)
        self.scan_progress_label.setText("已取消" if self._scan_cancel_requested else "就绪")
        self._scan_cancel_requested = False

    def cancel(self):
        self._scan_cancel_requested = True
        self.cancel_button.setEnabled(False)
        self.scan_progress_label.setText("正在安全停止当前任务…")

    def closeEvent(self, event):
        self._closing = True
        self.cancel()
        for page in (self.material_page, self.texture_page, self.import_page):
            stop = getattr(page, "stop", None)
            if stop:
                try:
                    stop(cancelled=True)
                except TypeError:
                    stop()
                except Exception as exc:
                    unreal.log_warning("[DatasmithBatchTools] 停止任务失败：{}".format(exc))
        self._save_state()
        super().closeEvent(event)


def show_window():
    global _window
    if _window is None:
        _window = BatchToolsWindow()
    _window.show()
    _window.raise_()
    _window.activateWindow()
    return _window


def register_menu():
    try:
        menus = unreal.ToolMenus.get()
        menu = menus.extend_menu("LevelEditor.MainMenu.Window")
        entry = unreal.ToolMenuEntry(name="DatasmithBatchTools.Open", type=unreal.MultiBlockType.MENU_ENTRY)
        entry.set_label("Datasmith Batch Tools")
        entry.set_tool_tip("打开批量材质、贴图与 Datasmith 工具")
        entry.set_string_command(unreal.ToolMenuStringCommandType.PYTHON, "", "import datasmith_batch_tools.ui as ui; ui.show_window()")
        menu.add_menu_entry("", entry)
        menus.refresh_all_widgets()
    except Exception as exc:
        unreal.log_warning("[DatasmithBatchTools] 菜单注册失败：{}".format(exc))
