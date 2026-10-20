# -*- coding: utf-8 -*-
"""Batch instance page, sharing the host's task lock and cancellation."""
from __future__ import annotations

import importlib
import traceback

import unreal
from PySide6.QtWidgets import (
    QAbstractItemView, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from . import material_instance_processor as processor, version

if processor.API_VERSION != version.MATERIAL_INSTANCE_API_VERSION:
    processor = importlib.reload(processor)

API_VERSION = "1.0.2"


class MaterialInstancePage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.job = None
        self.tick_handle = None
        self.plans = []
        self.issues = []
        self.scanned_config = None
        self._restoring = False
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        intro = QLabel("递归扫描 UE 贴图目录；每张 BaseColor / BaseColorAO 创建一个实例。同组其他贴图按 FileID 取第一张。已有实例跳过。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        settings = QGroupBox("目录与母材质")
        grid = QGridLayout(settings)
        grid.setColumnStretch(1, 1)
        self.source = QLineEdit(processor.DEFAULT_SOURCE)
        self.parent_path = QLineEdit(processor.DEFAULT_PARENT)
        self.output = QLineEdit(processor.DEFAULT_OUTPUT)
        for index, (label, edit, pick) in enumerate((
            ("UE 贴图根目录", self.source, lambda: self._pick_folder(self.source)),
            ("母材质", self.parent_path, self._pick_parent),
            ("实例输出根目录", self.output, lambda: self._pick_folder(self.output)),
        )):
            text = QLabel(label)
            text.setBuddy(edit)
            grid.addWidget(text, index, 0)
            edit.setAccessibleName(label)
            edit.setToolTip("输入 UE /Game 路径，或读取内容浏览器选择。")
            grid.addWidget(edit, index, 1)
            button = QPushButton("读取选中母材质" if index == 1 else "读取选中文件夹")
            button.clicked.connect(pick)
            grid.addWidget(button, index, 2)
        layout.addWidget(settings)
        output_help = QLabel("输出层级：输出根目录 / 属性分类 / 编号_分类 / 材质实例，例如 Materials/Asphalt/107_Asphalt/107_Bridge_Asphalt_01_001。")
        output_help.setWordWrap(True)
        layout.addWidget(output_help)

        defaults = QHBoxLayout()
        self.metal_default = QDoubleSpinBox()
        self.rough_default = QDoubleSpinBox()
        for label, spin, value in (("缺图金属度", self.metal_default, 0.0), ("缺图粗糙度", self.rough_default, 0.5)):
            text = QLabel(label)
            text.setBuddy(spin)
            defaults.addWidget(text)
            spin.setRange(0.0, 1.0)
            spin.setDecimals(3)
            spin.setSingleStep(0.05)
            spin.setValue(value)
            defaults.addWidget(spin)
        defaults.addWidget(QLabel("缺法线使用平坦法线；金属度读 B，粗糙度读 G，忽略 R/A。"))
        defaults.addStretch(1)
        layout.addLayout(defaults)

        actions = QHBoxLayout()
        self.scan_button = QPushButton("1. 扫描贴图并预览")
        self.scan_button.clicked.connect(self.scan)
        actions.addWidget(self.scan_button)
        self.create_button = QPushButton("2. 创建材质实例")
        self.create_button.setEnabled(False)
        self.create_button.clicked.connect(self.create)
        actions.addWidget(self.create_button)
        self.locate_source = QPushButton("定位基础色贴图")
        self.locate_source.clicked.connect(lambda: self._locate(False))
        actions.addWidget(self.locate_source)
        self.locate_target = QPushButton("定位材质实例")
        self.locate_target.clicked.connect(lambda: self._locate(True))
        actions.addWidget(self.locate_target)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.summary = QLabel("请先扫描。修改设置后需重新扫描。")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(("编号", "分类", "基础色", "法线", "金属度（B）", "粗糙度（G）", "目标实例（相对输出根目录）", "状态", "说明"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((65, 85, 160, 140, 140, 140, 290, 90, 220)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table, 1)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        self.log.document().setMaximumBlockCount(3000)
        layout.addWidget(self.log)
        for edit in (self.source, self.parent_path, self.output):
            edit.textChanged.connect(self._invalidate)
        for spin in (self.metal_default, self.rough_default):
            spin.valueChanged.connect(self._invalidate)

    def save_state(self):
        return dict(source=self.source.text(), parent=self.parent_path.text(), output=self.output.text(),
                    metal=self.metal_default.value(), rough=self.rough_default.value())

    def restore_state(self, state):
        self._restoring = True
        try:
            self.source.setText(str(state.get("source", processor.DEFAULT_SOURCE)))
            self.parent_path.setText(str(state.get("parent", processor.DEFAULT_PARENT)))
            self.output.setText(str(state.get("output", processor.DEFAULT_OUTPUT)))
            self.metal_default.setValue(float(state.get("metal", 0.0)))
            self.rough_default.setValue(float(state.get("rough", 0.5)))
        finally:
            self._restoring = False

    def _config(self):
        return processor.options(**self.save_state())

    def _invalidate(self, *_):
        if self._restoring:
            return
        self.scanned_config = None
        self.create_button.setEnabled(False)
        self.summary.setText("设置已修改，原预览失效，请重新扫描。")
        self.host._save_state()

    def _message(self, message, error=False):
        self.log.append(str(message))
        (unreal.log_error if error else unreal.log)("[MaterialInstances] " + str(message))

    def _error(self, title, exc):
        self._message(str(exc), True)
        unreal.log_error(traceback.format_exc())
        QMessageBox.warning(self, title, str(exc))

    def _pick_folder(self, edit):
        try:
            folders = list(unreal.EditorUtilityLibrary.get_selected_folder_paths())
            if not folders:
                folders = list(unreal.EditorUtilityLibrary.get_selected_path_view_folder_paths())
            if len(folders) != 1:
                raise ValueError("请在内容浏览器中只选择一个文件夹。")
            edit.setText(processor.game_directory(str(folders[0])))
        except Exception as exc:
            self._error("读取文件夹", exc)

    def _pick_parent(self):
        try:
            assets = list(unreal.EditorUtilityLibrary.get_selected_assets())
            if len(assets) != 1 or not isinstance(assets[0], unreal.Material):
                raise ValueError("请在内容浏览器中选择一个母材质（Material）。")
            self.parent_path.setText(processor.package_path(assets[0].get_path_name()))
        except Exception as exc:
            self._error("读取母材质", exc)

    def _locate(self, target):
        row = self.table.currentRow()
        if not 0 <= row < len(self.plans):
            return
        path = self.plans[row].target if target else self.plans[row].base
        if path and unreal.EditorAssetLibrary.does_asset_exist(path):
            unreal.EditorAssetLibrary.sync_browser_to_objects([path])
        else:
            self._message("目标实例尚未创建，或资产已经不存在。")

    def _start(self, job, title):
        if self.host._scan_active or self.job:
            raise RuntimeError("已有任务正在运行。")
        self.job = dict(job, busy=False, stop_requested=False)
        try:
            self.host._begin_scan(title)
            self.tick_handle = unreal.register_slate_post_tick_callback(self._tick)
        except Exception:
            self.stop(cancelled=True, failed=True)
            raise

    def scan(self):
        if self.host._scan_active or self.job:
            return
        try:
            config = self._config()
            self.host._save_state()
            self.scanned_config = None
            self.create_button.setEnabled(False)
            self.plans, self.issues = [], []
            self._populate()
            self.summary.setText("正在递归扫描 UE 贴图目录…")
            self._start(dict(kind="scan", config=config, iterator=processor.scan_textures(config)), "实例贴图扫描")
        except Exception as exc:
            self._error("扫描失败", exc)

    def create(self):
        if self.host._scan_active or self.job:
            return
        try:
            config = self._config()
            if config != self.scanned_config:
                raise ValueError("预览已失效，请重新扫描。")
            queue = [p for p in self.plans if p.status == "ready"]
            if not queue:
                raise ValueError("没有待创建的材质实例。")
            parent = processor.validate_parent(config["parent"])
            choice = QMessageBox.question(self, "创建材质实例", "将在 {} 创建 {} 个实例。已有资产跳过。\n是否继续？".format(config["output"], len(queue)), QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if choice != QMessageBox.StandardButton.Yes:
                return
            if config != self._config() or self.scanned_config != config:
                raise ValueError("设置已改变，请重新扫描。")
            batch = processor.begin_batch(config, self.plans, self.issues)
            self._message("批次报告：" + batch["stem"] + ".json")
            self._start(dict(kind="create", config=config, parent=parent, queue=queue, index=0, batch=batch), "创建材质实例")
        except Exception as exc:
            self._error("创建失败", exc)

    def _tick(self, _delta_seconds=0.0):
        job = self.job
        if not job or job["busy"]:
            return
        job["busy"] = True
        finish, cancelled, failed = False, False, False
        try:
            if self.host._scan_cancel_requested or self.host._closing or job["stop_requested"]:
                finish, cancelled = True, True
            elif job["kind"] == "scan":
                item = next(job["iterator"], None)
                if item is None:
                    finish = True
                else:
                    self.host._scan_progress(item["done"], item["total"])
                    if "plans" in item:
                        self.plans, self.issues = item["plans"], item["issues"]
                        self.scanned_config = dict(job["config"])
                        self._populate()
                        self._update_summary(item["summary"])
                        for issue in self.issues:
                            self._message(issue["path"] + "：" + issue["reason"])
                        finish = True
            elif job["index"] >= len(job["queue"]):
                finish = True
            else:
                plan = job["queue"][job["index"]]
                processor.create_one(plan, job["config"], job["parent"])
                job["index"] += 1
                processor.write_report(job["batch"])
                self._update_row(self.plans.index(plan))
                self._message("{}：{}；{}".format(processor.STATUS[plan.status], plan.target, plan.message), plan.status == "failed")
                self.host._scan_progress(job["index"], len(job["queue"]))
        except Exception as exc:
            self._message("任务停止：" + str(exc), True)
            unreal.log_error(traceback.format_exc())
            finish, failed = True, True
        finally:
            job["busy"] = False
            cancelled = cancelled or self.host._scan_cancel_requested or job["stop_requested"] or self.host._closing
            if finish or cancelled:
                self.stop(cancelled=cancelled, failed=failed)

    def stop(self, cancelled=False, failed=False):
        job = self.job
        if not job:
            return
        if job["busy"]:
            job["stop_requested"] = True
            return
        self.job = None
        handle, self.tick_handle = self.tick_handle, None
        try:
            if handle is not None:
                unreal.unregister_slate_post_tick_callback(handle)
            if job["kind"] == "scan":
                job["iterator"].close()
                if cancelled or failed:
                    self.scanned_config = None
                    self.summary.setText("扫描已停止，请重新扫描。")
                else:
                    self._message("扫描完成。检查匹配结果后可创建实例。")
            else:
                report = processor.finish_batch(job["batch"], cancelled, failed)
                self._message("任务{}；报告：{}".format("失败" if failed else "已取消" if cancelled else "完成", report))
                self._populate()
                self._update_summary()
        except Exception as exc:
            self._message("结束任务或写入报告失败：" + str(exc), True)
        finally:
            self.host._end_scan()
            self.create_button.setEnabled(self.scanned_config is not None and any(p.status == "ready" for p in self.plans))

    def _update_summary(self, scan=None):
        from collections import Counter
        counts = Counter(p.status for p in self.plans)
        text = "实例计划 {}；待创建 {}；已创建 {}；已有跳过 {}；冲突/无效 {}；失败 {}；扫描提示 {}".format(len(self.plans), counts["ready"], counts["created"], counts["existing"], counts["conflict"] + counts["invalid"], counts["failed"], len(self.issues))
        if scan:
            text = "贴图 {}；材质组 {}；".format(scan["textures"], scan["groups"]) + text
        self.summary.setText(text)

    def _populate(self):
        self.table.setRowCount(len(self.plans))
        for row in range(len(self.plans)):
            self._update_row(row)

    def _update_row(self, row):
        p = self.plans[row]
        config = self.scanned_config or (self.job or {}).get("config", {})
        values = (p.material_id, p.category, p.base, p.normal or "平坦法线", p.metal or "常量 {}".format(config.get("metal", self.metal_default.value())), p.rough or "常量 {}".format(config.get("rough", self.rough_default.value())), p.target, processor.STATUS[p.status], "；".join([p.message] + p.notes).strip("；"))
        for col, value in enumerate(values):
            short = value.rsplit("/", 1)[-1] if 2 <= col <= 5 else value
            if col == 6:
                root = config.get("output", self.output.text().strip()).rstrip("/") + "/"
                short = value[len(root):] if value.startswith(root) else value
            item = QTableWidgetItem(short)
            tooltip = value
            if col in (3, 4, 5):
                purpose = {3: "normal", 4: "metal", 5: "rough"}[col]
                tooltip += "\n候选（按选择顺序）：\n" + "\n".join(p.candidates.get(purpose, []))
            item.setToolTip(tooltip)
            self.table.setItem(row, col, item)
