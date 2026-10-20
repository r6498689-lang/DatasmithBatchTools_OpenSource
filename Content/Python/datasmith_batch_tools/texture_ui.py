# -*- coding: utf-8 -*-
"""Texture batch tab. UObject work runs one unit per editor Slate tick."""
from __future__ import annotations

import traceback
import unreal
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)
from . import texture_processor as processor

CHECKED = Qt.CheckState.Checked
UNCHECKED = Qt.CheckState.Unchecked
PROPERTY_LABELS = dict((key, value[0]) for key, value in processor.COLOR_FIELDS.items())
PROPERTY_LABELS.update(max_texture_size="尺寸上限", lod_bias="LOD Bias")
STATUS_LABELS = {"applied": "已应用", "restored": "已恢复", "skipped": "跳过", "failed": "失败", "prepared": "待完成", "rollback_failed": "回滚失败"}


class TextureBatchPage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.records = []
        self.job = None
        self.tick_handle = None
        self._updating = False
        self._build()
        self._refresh_history()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        intro = QLabel(
            "直接修改原 Texture2D 的 UE 属性，会影响所有引用。仅处理 /Game 资产；不改外部原图。"
            "调色需要勾选“确认颜色”，名称识别仅是候选，不是材质输入追踪。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        scan_row = QHBoxLayout()
        for label, source in (("1. 读取选中贴图", "selected_textures"), ("1. 收集选中模型及子级贴图", "selected_models")):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, source=source: self.scan(source))
            scan_row.addWidget(button)
        self.scope_label = QLabel("尚未扫描；与窗口上方锁定的 Datasmith Root 无关")
        self.scope_label.setWordWrap(True)
        scan_row.addWidget(self.scope_label, 1)
        layout.addLayout(scan_row)

        resolution = QGroupBox("降分辨率（绝对值覆盖，两个模式互斥）")
        row = QHBoxLayout(resolution)
        self.resolution_enabled = QCheckBox("启用降分辨率")
        row.addWidget(self.resolution_enabled)
        self.resolution_mode = QComboBox()
        self.resolution_mode.addItem("最大纹理尺寸", "max_size")
        self.resolution_mode.addItem("LOD Bias", "lod_bias")
        row.addWidget(self.resolution_mode)
        maximum_label = QLabel("最大尺寸")
        row.addWidget(maximum_label)
        self.maximum = QComboBox()
        for value in (256, 512, 1024, 2048, 4096, 8192, 16384):
            self.maximum.addItem(str(value), value)
        self.maximum.setCurrentIndex(self.maximum.findData(2048))
        maximum_label.setBuddy(self.maximum)
        row.addWidget(self.maximum)
        bias_label = QLabel("LOD Bias")
        row.addWidget(bias_label)
        self.bias = QComboBox()
        for value in range(13):
            self.bias.addItem(str(value), value)
        self.bias.setCurrentIndex(1)
        bias_label.setBuddy(self.bias)
        row.addWidget(self.bias)
        row.addStretch(1)
        layout.addWidget(resolution)

        colors = QGroupBox("颜色调整（只改勾选参数；每批共用一套数值）")
        color_layout = QGridLayout(colors)
        self.color_controls = {}
        tips = {
            "adjust_brightness": "HSV 明度乘数：1 保持原值，0 变黑。不是曝光 EV。",
            "adjust_hue": "HSV 色相角度偏移：单位为度，0 不偏移，360 循环一周。",
            "adjust_saturation": "饱和度乘数：1 保持原值，0 灰度。",
            "adjust_vibrance": "自然饱和度：0 不额外增强，1 为最大增强；不等同于饱和度乘数。",
        }
        for index, (key, (label, default, minimum, maximum)) in enumerate(processor.COLOR_FIELDS.items()):
            check = QCheckBox("启用" + label)
            check.setToolTip(tips[key])
            spin = QDoubleSpinBox()
            spin.setRange(minimum, maximum)
            spin.setDecimals(3 if key != "adjust_hue" else 1)
            spin.setSingleStep(0.05 if key != "adjust_hue" else 5.0)
            spin.setValue(default)
            if key == "adjust_hue":
                spin.setSuffix("°")
            spin.setEnabled(False)
            spin.setAccessibleName(label)
            spin.setToolTip(tips[key])
            self.color_controls[key] = (check, spin)
            check.toggled.connect(lambda enabled, spin=spin: spin.setEnabled(enabled))
            check.toggled.connect(self._update_plans)
            spin.valueChanged.connect(self._update_plans)
            color_layout.addWidget(check, index // 2, (index % 2) * 2)
            color_layout.addWidget(spin, index // 2, (index % 2) * 2 + 1)
        layout.addWidget(colors)

        help_text = QLabel(
            "最大尺寸模式会将资产 LOD Bias 设为 0；LOD Bias 模式会将最大尺寸设为 0（无限制）。"
            "不改 Texture Group / Device Profile / mip 算法。预计尺寸不是实际驻留尺寸；无 mip 时跳过 LOD Bias。"
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        tools = QHBoxLayout()
        for label, checked in (("全选可处理贴图", True), ("取消全部处理勾选", False)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, checked=checked: self._check_processing(checked))
            tools.addWidget(button)
        locate = QPushButton("定位当前行贴图")
        locate.clicked.connect(self._locate)
        tools.addWidget(locate)
        tools.addStretch(1)
        self.plan_summary = QLabel("未启用处理参数")
        tools.addWidget(self.plan_summary)
        layout.addLayout(tools)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(("执行", "确认颜色", "贴图", "源图尺寸", "当前上限 / Bias", "预计尺寸", "用途与依据", "参数改动摘要", "状态"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate((50, 80, 155, 100, 120, 100, 160, 240, 80)):
            self.table.setColumnWidth(column, width)
        self.table.cellChanged.connect(self._update_plans)
        layout.addWidget(self.table, 1)

        action = QHBoxLayout()
        trial = QPushButton("2. 单张试应用（当前行）")
        trial.clicked.connect(lambda: self.apply(trial=True))
        action.addWidget(trial)
        apply_button = QPushButton("3. 应用全部勾选贴图")
        apply_button.clicked.connect(lambda: self.apply(trial=False))
        action.addWidget(apply_button)
        action.addStretch(1)
        layout.addLayout(action)
        recovery = QHBoxLayout()
        history_label = QLabel("恢复批次")
        recovery.addWidget(history_label)
        self.history = QComboBox()
        history_label.setBuddy(self.history)
        self.history.setMinimumContentsLength(20)
        self.history.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        recovery.addWidget(self.history, 1)
        refresh = QPushButton("刷新历史")
        refresh.clicked.connect(self._refresh_history)
        recovery.addWidget(refresh)
        restore = QPushButton("恢复所选批次")
        restore.clicked.connect(self.restore)
        recovery.addWidget(restore)
        layout.addLayout(recovery)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(90)
        layout.addWidget(self.log)
        for control in (self.resolution_enabled,):
            control.toggled.connect(self._update_plans)
        for control in (self.resolution_mode, self.maximum, self.bias):
            control.currentIndexChanged.connect(self._update_plans)
        self._update_plans()

    def _message(self, text, error=False):
        self.log.append(str(text))
        (unreal.log_error if error else unreal.log)("[TextureBatch] " + str(text))

    def _options(self):
        return {
            "resolution_enabled": self.resolution_enabled.isChecked(),
            "resolution_mode": self.resolution_mode.currentData(),
            "max_texture_size": self.maximum.currentData(),
            "lod_bias": self.bias.currentData(),
            "colors": {key: spin.value() for key, (check, spin) in self.color_controls.items() if check.isChecked()},
        }

    def _color_confirmed(self, row):
        return self.table.item(row, 1).checkState() == CHECKED

    def _update_plans(self, *_args):
        if self._updating:
            return
        self._updating = True
        self.table.blockSignals(True)
        try:
            options = self._options()
            enabled = options["resolution_enabled"]
            self.resolution_mode.setEnabled(enabled)
            self.maximum.setEnabled(enabled and options["resolution_mode"] == "max_size")
            self.bias.setEnabled(enabled and options["resolution_mode"] == "lod_bias")
            count, fields = 0, 0
            for row, record in enumerate(self.records):
                if not enabled and not options["colors"]:
                    plan = {"after": {}, "before": {}, "warnings": []}
                else:
                    plan = processor.plan_record(record, options, self._color_confirmed(row))
                width, height = processor.estimated_size(record, plan["after"])
                self.table.item(row, 5).setText("{} × {}（估算）".format(width, height) if record.source_width else "未知")
                summary = "；".join("{} {} → {}".format(PROPERTY_LABELS[key], plan["before"][key], value) for key, value in plan["after"].items()) or "无改动"
                self.table.item(row, 7).setText(summary)
                self.table.item(row, 7).setToolTip(summary + "\n" + "\n".join(plan["warnings"]))
                if self.table.item(row, 0).checkState() == CHECKED and plan["after"]:
                    count += 1
                    fields += len(plan["after"])
            self.plan_summary.setText("计划修改 {} 张 / {} 项参数".format(count, fields))
        finally:
            self.table.blockSignals(False)
            self._updating = False

    def _populate(self, confirmations=None, processing=None):
        confirmations, processing = confirmations or set(), processing or set()
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.records))
        for row, record in enumerate(self.records):
            texts = ("", "", record.name, "{} × {}".format(record.source_width, record.source_height),
                     "{} / {}".format(record.values["max_texture_size"], record.values["lod_bias"]), "", record.reason, "", record.status)
            for column, text in enumerate(texts):
                item = QTableWidgetItem(str(text))
                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(record.path + "\n" + record.reason + "\n引用材质（扫描范围内）：\n" + "\n".join(record.materials) + "\n参数提示：" + ", ".join(record.hints))
                self.table.setItem(row, column, item)
            for column, allowed, checked in (
                (0, record.editable, record.path in processing),
                (1, record.editable and record.kind != "data", record.path in confirmations),
            ):
                item = self.table.item(row, column)
                if allowed:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(CHECKED if checked else UNCHECKED)
                else:
                    item.setText("—")
                    item.setFlags(Qt.ItemFlag.ItemIsSelectable)
        self.table.blockSignals(False)
        self._update_plans()

    def _check_processing(self, checked):
        self.table.blockSignals(True)
        for row, record in enumerate(self.records):
            if record.editable:
                self.table.item(row, 0).setCheckState(CHECKED if checked else UNCHECKED)
        self.table.blockSignals(False)
        self._update_plans()

    def _locate(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.records):
            unreal.EditorAssetLibrary.sync_browser_to_objects([self.records[row].path])
        else:
            QMessageBox.information(self, "定位贴图", "请先选择一行贴图。")

    def _start(self, job, title):
        if self.host._scan_active or self.job:
            raise RuntimeError("已有任务正在运行")
        processor.native_library()
        self.job = dict(job, busy=False, cooldown=0)
        self.host._begin_scan(title)
        try:
            self.tick_handle = unreal.register_slate_post_tick_callback(self._tick)
        except Exception:
            self.job = None
            self.host._end_scan()
            raise

    def scan(self, source):
        try:
            if self.host._scan_active or self.job:
                return
            self.records = []
            self._populate()
            self.scope_label.setText("正在读取当前选择（不会扫描整个项目）")
            # Freeze the click-time selection, rather than read a different UE
            # selection later when the generator first runs on a Slate tick.
            if source == "selected_textures":
                iterator = processor.scan_items(source, selected_assets=list(unreal.EditorUtilityLibrary.get_selected_assets()))
            else:
                iterator = processor.scan_items(source, selected_actors=list(unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_selected_level_actors()))
            self._start({"kind": "scan", "iterator": iterator, "source": source, "index": 0}, "贴图扫描")
        except Exception as exc:
            self._message(str(exc), True)
            QMessageBox.warning(self, "贴图扫描", str(exc))

    def apply(self, trial=False):
        try:
            options = processor.validate_options(self._options())
            if trial:
                row = self.table.currentRow()
                if not 0 <= row < len(self.records):
                    raise ValueError("请先选择一行作为试应用贴图")
                rows = [row]
            else:
                rows = [row for row in range(len(self.records)) if self.table.item(row, 0).checkState() == CHECKED]
            queue, details, skipped = [], [], 0
            for row in rows:
                record = self.records[row]
                plan = processor.plan_record(record, options, self._color_confirmed(row))
                if plan["after"]:
                    queue.append((row, record, self._color_confirmed(row)))
                    details.append(record.name + "：" + self.table.item(row, 7).text())
                else:
                    skipped += 1
            if not queue:
                raise ValueError("没有可执行改动。请启用参数、勾选执行；调色还需要勾选“确认颜色”。参数已相同时不会重复修改。")
            message = (
                "将{} {} 张原贴图并逐张保存；{} 张无改动/不满足条件。\n"
                "所有引用它们的材质都会受到影响。\n"
                "取消仅停止后续项，不能中断正在重建的单张。\n\n{}{}\n\n是否继续？"
            ).format("试应用" if trial else "修改", len(queue), skipped, "\n".join(details[:8]), "\n……其余见表格摘要" if len(details) > 8 else "")
            if QMessageBox.question(self, "确认贴图修改", message, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            batch = processor.new_batch(options, "trial" if trial else "apply")
            self._start({"kind": "apply", "queue": queue, "index": 0, "options": options, "batch": batch}, "贴图处理")
            self._message("修改前记录：" + processor.write_batch(batch))
        except Exception as exc:
            self._message(str(exc), True)
            QMessageBox.warning(self, "贴图处理", str(exc))

    def _refresh_history(self):
        current_id = self.history.currentData()
        self.history.clear()
        try:
            for batch in processor.list_history():
                count = sum(entry.get("status") in ("applied", "prepared", "restored", "rollback_failed") for entry in batch["entries"])
                if count:
                    label = "{} | {} | {} 张 | {}".format(batch["started_at"], {"apply": "整批", "trial": "试应用", "restore": "恢复操作"}.get(batch["mode"], batch["mode"]), count, batch["status"])
                    self.history.addItem(label, batch["batch_id"])
                    self.history.setItemData(self.history.count() - 1, batch["batch_id"], Qt.ItemDataRole.ToolTipRole)
            index = self.history.findData(current_id)
            if index >= 0:
                self.history.setCurrentIndex(index)
        except Exception as exc:
            self._message("历史读取失败：" + str(exc), True)

    def restore(self):
        try:
            batch_id = self.history.currentData()
            if not batch_id:
                raise ValueError("没有可恢复的批次")
            source = next((batch for batch in processor.list_history() if batch["batch_id"] == batch_id), None)
            if not source:
                raise ValueError("历史记录已不存在，请刷新")
            queue = [entry for entry in source["entries"] if entry.get("status") in ("applied", "prepared", "restored", "rollback_failed")]
            message = "检查并恢复 {} 张贴图本批次改过的参数。\n相关参数后续已改动、源图已变化或资产丢失的会跳过。\n不会恢复外部原图；恢复结果将逐张保存。\n\n是否继续？".format(len(queue))
            if QMessageBox.question(self, "确认恢复", message, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            batch = processor.new_batch({}, "restore", source["batch_id"])
            self._start({"kind": "restore", "queue": queue, "index": 0, "batch": batch}, "贴图恢复")
        except Exception as exc:
            self._message(str(exc), True)
            QMessageBox.warning(self, "恢复贴图", str(exc))

    def _tick(self, _delta_seconds=0.0):
        job = self.job
        if job is None or job["busy"]:
            return
        if self.host._scan_cancel_requested:
            self.stop(cancelled=True)
            return
        if job["cooldown"]:
            job["cooldown"] -= 1
            return
        job["busy"] = True
        try:
            if job["kind"] == "scan":
                try:
                    item = next(job["iterator"])
                except StopIteration:
                    self.stop()
                    return
                if "record" in item:
                    self.records.append(item["record"])
                if "error" in item:
                    self._message(item["error"], True)
                if "warning" in item:
                    self._message(item["warning"])
                self.host.scan_progress_label.setText("贴图扫描：{} / {} {}".format(item.get("done", "收集"), item.get("total", ""), item.get("message", "")))
            else:
                index, queue = job["index"], job["queue"]
                if index >= len(queue):
                    self.stop()
                    return
                self.host.scan_progress_label.setText("贴图{}：{} / {}（正在重建并保存）".format("恢复" if job["kind"] == "restore" else "处理", index + 1, len(queue)))
                if job["kind"] == "restore":
                    entry = processor.restore_one(job["batch"], queue[index])
                else:
                    row, record, confirmed = queue[index]
                    entry = processor.process_one(job["batch"], record, job["options"], confirmed)
                    record.status = STATUS_LABELS.get(entry["status"], entry["status"])
                    # Refresh even failures: rollback may have failed. Never display stale values.
                    try:
                        asset = unreal.EditorAssetLibrary.load_asset(record.path)
                        updated = processor.inspect_texture(asset, record.materials, record.hints)
                        updated.status = record.status
                        self.records[row] = updated
                    except Exception:
                        pass
                job["index"] += 1
                self._message("[{status}] {name}：{message}{warnings}".format(status=STATUS_LABELS.get(entry["status"], entry["status"]), name=entry.get("name") or entry["path"], message=entry["message"], warnings="；" + "；".join(entry.get("warnings", [])) if entry.get("warnings") else ""), entry["status"] in ("failed", "rollback_failed"))
        except Exception as exc:
            self._message("任务已停止：" + str(exc), True)
            unreal.log_error(traceback.format_exc())
            self.stop(cancelled=True)
        finally:
            job["busy"] = False
            job["cooldown"] = 1

    def stop(self, cancelled=False):
        job, self.job = self.job, None
        handle, self.tick_handle = self.tick_handle, None
        if handle is not None:
            unreal.unregister_slate_post_tick_callback(handle)
        if job is None:
            return
        try:
            if job["kind"] == "scan":
                job["iterator"].close()
                self._populate(processing={record.path for record in self.records if record.editable})
                self.scope_label.setText("{}：{} 张唯一贴图{}".format("选中贴图" if job["source"] == "selected_textures" else "选中模型及子级", len(self.records), "（扫描已取消，清单不完整）" if cancelled else ""))
                self._message("扫描{}：{} 张；颜色确认默认全部未勾选。".format("取消" if cancelled else "完成", len(self.records)))
            else:
                report = processor.finish_batch(job["batch"], cancelled)
                confirmations = {record.path for row, record in enumerate(self.records) if self._color_confirmed(row)}
                processing = {record.path for row, record in enumerate(self.records) if self.table.item(row, 0).checkState() == CHECKED}
                if job["kind"] == "restore":
                    for row, record in enumerate(self.records):
                        try:
                            asset = unreal.EditorAssetLibrary.load_asset(record.path)
                            self.records[row] = processor.inspect_texture(asset, record.materials, record.hints)
                        except Exception:
                            pass
                self._populate(confirmations, processing)
                summary = "，".join("{} {}".format(STATUS_LABELS.get(key, key), value) for key, value in job["batch"].get("summary", {}).items()) or "未处理任何项"
                self._message("任务{}：{}；JSON 恢复记录：{}".format("已取消" if cancelled else "完成", summary, report))
                self._refresh_history()
        except Exception as exc:
            self._message("结束任务时发生错误，已有修改前 JSON 记录仍保留：" + str(exc), True)
        finally:
            self.host._end_scan()
