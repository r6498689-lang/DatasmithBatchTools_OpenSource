# -*- coding: utf-8 -*-
"""Serial Datasmith queue. Import and Save All are distinct, ordered steps."""
from __future__ import annotations

import csv
import io
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import unreal

from . import storage

API_VERSION = "1.2.0"
MODES = {"normal": "普通模式", "large": "大文件模式"}
DEFAULT_OUTPUT = "/Game/DatasmithImports"
MAP_NAME = "批量导入场景"
STATUS = {
    "waiting": "等待导入", "importing": "正在导入", "saving": "等待全部保存",
    "completed": "导入并保存完成", "import_failed": "导入失败", "save_failed": "保存失败",
    "cancelled": "未执行（已取消）", "invalid": "文件无效",
    "compiling": "等待编译完成", "cleaning": "清理临时资源", "maintenance_failed": "资源处理失败",
    "waiting_materials": "等待材质阶段", "validating": "恢复并校验材质", "incomplete": "未完整导入",
    "waiting_geometry": "等待下一批几何体",
}


def game_directory(value):
    value = str(value).strip().replace("\\", "/").rstrip("/")
    if value.startswith("/All/"):
        value = value[4:]
    parts = value.split("/")
    if len(parts) < 3 or parts[:2] != ["", "Game"] or any(
        not p or not all(c.isalnum() or c == "_" for c in p) for p in parts[2:]
    ):
        raise ValueError("请输入 /Game 下的 UE 输出目录；各层名称使用文字、数字或下划线。")
    return value


def safe_name(value):
    name = "".join(c if c.isalnum() or c == "_" else "_" for c in str(value))[:96]
    return name if name and name.strip("_") else "DatasmithScene"


@dataclass
class ImportItem:
    source: str
    original_name: str
    name: str
    target: str
    status: str = "waiting"
    message: str = ""
    scene: str = ""
    actors: int = 0
    import_error: str = ""
    save_error: str = ""
    started: str = ""
    finished: str = ""
    prepare_error: str = ""
    cleanup_error: str = ""
    memory_before: dict = field(default_factory=dict)
    memory_after: dict = field(default_factory=dict)
    memory_warning: str = ""
    stage: str = ""
    stage_history: list = field(default_factory=list)
    binding_meshes: list = field(default_factory=list)
    binding_index: int = 0
    checked_slots: int = 0
    repaired_slots: int = 0
    unassigned_slots: int = 0
    binding_issues: list = field(default_factory=list)
    validation_started: bool = False
    validation_completed: bool = False
    verification_index: int = 0
    source_signature: list = field(default_factory=list)
    geometry_index: int = 0
    geometry_total: int = 0
    texture_index: int = 0
    texture_total: int = 0


def now():
    return datetime.now().isoformat(timespec="seconds")


def unique_name(root, base, reserved, occupied):
    candidate, suffix = base, 0
    while (root + "/" + candidate).casefold() in reserved or occupied(root + "/" + candidate):
        suffix += 1
        candidate = "{}_{}".format(base, suffix)
    reserved.add((root + "/" + candidate).casefold())
    return candidate


def build_queue(files, root, occupied):
    root = game_directory(root)
    reserved, items = set(), []
    for value in files:
        source = os.path.abspath(str(value))
        stem = Path(source).stem
        name = unique_name(root, safe_name(stem), reserved, occupied)
        item = ImportItem(source, stem, name, root + "/" + name)
        if Path(source).suffix.lower() != ".udatasmith" or not os.path.isfile(source):
            item.status, item.message = "invalid", "文件不存在或扩展名不是 .udatasmith。"
        items.append(item)
    return items


class UnrealBackend:
    """Small UE boundary; all mutations run on the editor/game thread."""
    def __init__(self):
        if not hasattr(unreal, "DatasmithBatchImportLibrary") or not hasattr(unreal, "DatasmithImportOptions"):
            raise RuntimeError("原生批量导入接口尚未加载。请关闭并重启 UE，并确认已启用 Datasmith Importer。")
        required = ('release_temporary_resources', 'release_saved_source_cache', 'import_file_stage',
                    'get_scene_mesh_ids', 'check_material_bindings', 'validate_scene_textures', 'import_geometry_chunk',
                    'import_texture_chunk', 'import_materials_only')
        if not all(hasattr(unreal.DatasmithBatchImportLibrary, name) for name in required):
            raise RuntimeError("请重启 UE，加载 0.13.0 的分阶段导入、材质校验与资源清理接口。")
        self.world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        if not self.world:
            raise RuntimeError("请先打开 UE 编辑器关卡。")
        self.check_world()

    def check_world(self):
        if unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world():
            raise RuntimeError("请先停止运行或模拟，再执行 Datasmith 导入和保存。")
        current = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        if current != self.world:
            raise RuntimeError("当前关卡已切换，当前文件未导入。请等待批次结束后重新选择文件。")

    def occupied(self, path):
        library = unreal.EditorAssetLibrary
        if library.does_asset_exist(path) or library.does_directory_exist(path):
            return True
        # Also reserve directories/assets not yet indexed in the Content Browser.
        disk = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())) / path[len("/Game/"):]
        return disk.exists() or disk.with_suffix(".uasset").exists() or disk.with_suffix(".umap").exists()

    def import_file(self, item, root, settings):
        self.check_world()
        result = unreal.DatasmithBatchImportLibrary.import_file(item.source, root, item.name, settings)
        if not result.success:
            raise RuntimeError(str(result.error) or "Datasmith 返回导入失败；详细信息见 UE 输出日志。")
        if not result.scene:
            raise RuntimeError("Datasmith 未返回场景资源。")
        if result.actor_count:
            raise RuntimeError("导入意外产生了关卡 Actor，不能标记为仅导入资产成功。")
        return {"scene": result.scene.get_path_name(), "actors": int(result.actor_count)}

    def finish_compilation(self):
        unreal.DatasmithBatchImportLibrary.finish_compilation()

    def validate_mode(self, mode, settings):
        if mode != "large":
            return
        if not hasattr(unreal.DatasmithBatchImportLibrary, 'import_file_stage'):
            raise RuntimeError("请重启 UE，加载大文件模式的原生接口。")
        options = settings.get_editor_property('base_options')
        if not options.include_geometry or not options.include_material:
            raise ValueError("大文件模式需要同时勾选几何体和材质，脚本会自动分阶段导入。")

    def import_stage(self, item, root, settings):
        self.check_world()
        if item.stage == 'geometry':
            result = unreal.DatasmithBatchImportLibrary.import_geometry_chunk(
                item.source, root, item.name, settings, item.geometry_index, 24)
        elif item.stage == 'textures':
            result = unreal.DatasmithBatchImportLibrary.import_texture_chunk(
                item.source, root, item.name, settings, item.texture_index, 8)
        else:
            result = unreal.DatasmithBatchImportLibrary.import_materials_only(item.source, root, item.name, settings)
        if not result.success or not result.scene:
            raise RuntimeError(str(result.error) or "分阶段导入未返回有效场景。")
        if result.actor_count:
            raise RuntimeError("分阶段导入意外产生了关卡 Actor，不能标记为仅导入资产成功。")
        return {"scene": result.scene.get_path_name(), "actors": int(result.actor_count),
                "geometry_index": int(result.next_mesh_index), "geometry_total": int(result.total_meshes),
                "texture_index": int(result.next_texture_index), "texture_total": int(result.total_textures)}

    def release_source_cache(self, root):
        if not hasattr(unreal.DatasmithBatchImportLibrary, 'release_saved_source_cache'):
            raise RuntimeError("请重启 UE，加载新版源数据缓存清理接口。")
        return int(unreal.DatasmithBatchImportLibrary.release_saved_source_cache(root))

    def binding_mesh_ids(self, item):
        return list(unreal.DatasmithBatchImportLibrary.get_scene_mesh_ids(item.scene))

    def texture_issues(self, item):
        return [str(s) for s in unreal.DatasmithBatchImportLibrary.validate_scene_textures(item.scene)]

    def bind_mesh(self, item, mesh_id, repair=True):
        self.check_world()
        native = unreal.DatasmithBatchImportLibrary.check_material_bindings(item.scene, mesh_id, repair)
        result = {'checked': int(native.checked_slots), 'repaired': int(native.repaired_slots),
                  'unassigned': int(native.unassigned_slots), 'issues': [str(s) for s in native.issues]}
        path = str(native.mesh_path)
        # Persist one mesh before collecting. No UObject is kept in the session or report.
        self.finish_compilation()
        if path and not unreal.EditorAssetLibrary.save_asset(path, only_if_is_dirty=True):
            result['issues'].append("材质绑定后的网格保存失败：" + path)
        return result

    def check_mesh(self, item, mesh_id):
        return self.bind_mesh(item, mesh_id, repair=False)

    def cleanup(self):
        unreal.DatasmithBatchImportLibrary.release_temporary_resources()

    def memory_status(self):
        info = unreal.DatasmithBatchImportLibrary.get_memory_status()
        return {key: float(getattr(info, key)) for key in
                ("available_physical_gb", "available_virtual_gb", "used_physical_gb", "used_virtual_gb")}

    def save_all(self, root):
        self.check_world()
        errors = []
        package = self.world.get_path_name().split(".", 1)[0]
        if package.startswith(("/Temp/", "/Memory/", "/Transient")) or not package.startswith("/"):
            name = unique_name(root, MAP_NAME, set(), self.occupied)
            path = root + "/" + name
            try:
                if not unreal.EditorLoadingAndSavingUtils.save_map(self.world, path):
                    raise RuntimeError("关卡首次保存失败：" + path)
            except Exception as exc:
                errors.append(str(exc))
        # Even when the map could not be saved, attempt to save the imported assets.
        try:
            if not unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True):
                errors.append("UE 全部保存返回失败。")
            dirty = list(unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()) + list(
                unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages())
            if dirty:
                errors.append("仍有未保存资源：" + "、".join(p.get_path_name() for p in dirty))
        except Exception as exc:
            errors.append(str(exc))
        if errors:
            raise RuntimeError("；".join(errors))
        return self.world.get_path_name().split(".", 1)[0]


class BatchSession:
    """Assets only: import -> compile -> save -> cleanup -> 5s -> next. Low memory only warns."""
    def __init__(self, items, root, settings, backend, clock=None, min_physical_gb=2.0, min_virtual_gb=4.0, mode="normal"):
        if mode not in MODES:
            raise ValueError("未知导入模式：" + str(mode))
        self.mode = mode
        self.next_phase = "import"
        self.items, self.root, self.settings, self.backend = items, game_directory(root), settings, backend
        self.index, self.phase, self.done = 0, "import", False
        self.cancelled, self.stop_requested = False, False
        self.clock = clock or time.monotonic
        self.deadline = 0.0
        self.min_physical_gb, self.min_virtual_gb = min_physical_gb, min_virtual_gb
        self.memory = {}
        self.warning = ""
        self.journal = None
        self.map_path = ""
        self.started, self.finished = now(), ""
        self.report_name = "datasmith_import_batches/{}_{}".format(datetime.now().strftime("%Y%m%d_%H%M%S"), uuid4().hex[:8])

    def step(self, cancel=False):
        self.stop_requested = self.stop_requested or cancel
        if self.done:
            return
        if self.phase == "validate":
            self._validate_step()
            return
        if self.phase == "compile":
            item = self.items[self.index]
            try:
                self.backend.finish_compilation()
            except Exception as exc:
                item.prepare_error = str(exc)
            item.status, self.phase = "saving", "save"
        elif self.phase == "save":
            item = self.items[self.index]
            try:
                self.map_path = self.backend.save_all(self.root)
            except Exception as exc:
                item.save_error = str(exc)
            item.status, self.phase = "cleaning", "cleanup"
        elif self.phase == "cleanup":
            item = self.items[self.index]
            try:
                if not item.save_error and hasattr(self.backend, 'release_source_cache'):
                    self.backend.release_source_cache(item.target)
                self.backend.cleanup()
            except Exception as exc:
                item.cleanup_error = str(exc)
            self._memory_check(item, after=True)
            failed = bool(item.import_error or item.save_error or item.prepare_error or item.cleanup_error)
            if self.mode == 'large' and not failed and not self.stop_requested:
                if item.stage == 'geometry':
                    if item.geometry_index < item.geometry_total:
                        item.stage_history.append('geometry_chunk_saved_and_cleaned:{}'.format(item.geometry_index))
                        item.status = 'waiting_geometry'
                        item.message = '几何体 {}/{} 已保存并清理；即将处理下一批。'.format(item.geometry_index, item.geometry_total)
                        self.phase, self.next_phase, self.deadline = 'cooldown', 'import', self.clock() + 5.0
                        return
                    item.stage_history.append('geometry_saved_and_cleaned')
                    item.status, item.message = 'waiting_materials', '几何体已保存并清理；即将自动分批导入贴图，再导入材质。'
                    self.phase, self.next_phase, self.deadline = 'cooldown', 'import_textures', self.clock() + 5.0
                    return
                if item.stage == 'textures':
                    item.stage_history.append('textures_saved_and_cleaned:{}'.format(item.texture_index))
                    more_textures = item.texture_index < item.texture_total
                    item.status = 'waiting_materials'
                    item.message = '贴图 {}/{} 已保存并清理；即将{}。'.format(
                        item.texture_index, item.texture_total, '处理下一批贴图' if more_textures else '导入材质')
                    self.phase, self.next_phase, self.deadline = 'cooldown', ('import_textures' if more_textures else 'import_materials'), self.clock() + 5.0
                    return
                if item.stage == 'materials':
                    item.stage_history.append('materials_saved_and_cleaned')
                    item.status, item.message = 'validating', '材质和贴图已保存并清理；即将逐网格恢复并校验材质。'
                    self.phase, self.next_phase, self.deadline = 'cooldown', 'validate', self.clock() + 5.0
                    return
            item.status = ("import_failed" if item.import_error else "save_failed" if item.save_error else
                           "maintenance_failed" if item.prepare_error or item.cleanup_error else
                           "incomplete" if self.mode == 'large' and (item.binding_issues or not item.validation_completed) else "completed")
            item.message = "；".join(s for s in (item.import_error, item.prepare_error, item.save_error, item.cleanup_error) if s)
            if item.status == 'incomplete':
                item.message = '未完整导入：' + ('；'.join(item.binding_issues[:10]) or '批次已取消，材质绑定校验尚未完成。')
            elif not item.message:
                item.message = ('大文件导入完成；材质绑定校验通过，检查 {} 个槽，修复 {} 个槽；已全部保存并清理。'.format(item.checked_slots, item.repaired_slots)
                                if self.mode == 'large' else "仅导入资产完成，已等待编译、全部保存并清理临时资源。")
                if self.mode == 'large' and item.unassigned_slots:
                    item.message += '源文件本来未分配材质的槽：{} 个。'.format(item.unassigned_slots)
            item.finished = now()
            self.index += 1
            self.phase, self.next_phase, self.deadline = "cooldown", "import", self.clock() + 5.0
        elif self.stop_requested:
            self._cancel_remaining()
        elif self.index >= len(self.items):
            self._finish()
        elif self.phase == "cooldown":
            if self.clock() >= self.deadline:
                self.phase = self.next_phase
        else:
            item = self.items[self.index]
            if item.status == "invalid":
                self.index += 1
                return
            self._memory_check(item)
            material_phase = self.phase == 'import_materials'
            texture_phase = self.phase == 'import_textures'
            continuation = material_phase or texture_phase or (self.mode == 'large' and item.geometry_index > 0)
            item.stage = 'materials' if material_phase else ('textures' if texture_phase else ('geometry' if self.mode == 'large' else 'all'))
            item.started, item.status = item.started or now(), "importing"
            item.stage_history.append('import_' + item.stage)
            # Persist BEFORE entering C++: a fatal UE crash cannot return to Python.
            try:
                if self.journal:
                    self.journal()
                else:
                    self.write_report()
            except Exception:
                item.status = ("waiting_materials" if material_phase or texture_phase else "waiting_geometry") if continuation else "waiting"
                self.stop_requested = True
                self._cancel_remaining()
                raise
            if self.stop_requested:
                item.status = ("waiting_materials" if material_phase or texture_phase else "waiting_geometry") if continuation else "waiting"
                self._cancel_remaining()
                return
            try:
                if Path(item.source).suffix.lower() != ".udatasmith" or not os.path.isfile(item.source):
                    raise RuntimeError("源文件已经不存在或类型无效。")
                source_stat = os.stat(item.source)
                signature = [source_stat.st_size, source_stat.st_mtime_ns]
                if continuation and signature != item.source_signature:
                    raise RuntimeError('两阶段之间源 Datasmith 文件发生变化，已停止当前文件，避免材质与几何体错配。')
                if not continuation:
                    item.source_signature = signature
                # The output may have changed since preview; never reuse an occupied folder.
                if not continuation and self.backend.occupied(item.target):
                    reserved = {p.target.casefold() for i, p in enumerate(self.items) if i != self.index}
                    item.name = unique_name(self.root, safe_name(item.original_name), reserved, self.backend.occupied)
                    item.target = self.root + "/" + item.name
                result = (self.backend.import_stage(item, self.root, self.settings) if self.mode == 'large'
                          else self.backend.import_file(item, self.root, self.settings))
                item.scene, item.actors = result["scene"], result["actors"]
                if item.stage == 'geometry':
                    next_index, total = result.get('geometry_index', 0), result.get('geometry_total', 0)
                    if total and (not item.geometry_index < next_index <= total
                                  or item.geometry_total not in (0, total)):
                        raise RuntimeError('几何体分批进度异常，已停止当前文件。')
                    item.geometry_index, item.geometry_total = next_index, total
                if item.stage == 'textures':
                    next_index, total = result.get('texture_index', 0), result.get('texture_total', 0)
                    if total and (not item.texture_index < next_index <= total or item.texture_total not in (0, total)):
                        raise RuntimeError('贴图分批进度异常，已停止当前文件。')
                    item.texture_index, item.texture_total = next_index, total
            except Exception as exc:
                item.import_error = str(exc)
            item.status, self.phase = "compiling", "compile"
            item.message = (item.import_error + "；" if item.import_error else "资产导入完成；") + "下一步等待资源编译，再全部保存。"
        if self.stop_requested and self.phase not in ("compile", "save", "cleanup"):
            self._cancel_remaining()
        elif self.index >= len(self.items) and self.phase not in ("compile", "save", "cleanup"):
            self._finish()

    def _validate_step(self):
        item = self.items[self.index]
        if self.stop_requested:
            item.binding_issues.append('已取消，材质绑定校验尚未全部完成。')
            item.stage, item.status, self.phase = 'validation', 'compiling', 'compile'
            return
        if not item.validation_started:
            item.validation_started = True
            item.stage_history.append('restore_and_validate_materials')
            try:
                item.binding_meshes = list(self.backend.binding_mesh_ids(item))
                item.binding_issues.extend(self.backend.texture_issues(item))
                if not item.binding_meshes:
                    item.binding_issues.append('未找到可校验的源网格体，无法确认几何体完整性。')
            except Exception as exc:
                item.binding_issues.append(str(exc))
            return
        if item.binding_index < len(item.binding_meshes):
            mesh_id = item.binding_meshes[item.binding_index]
            # Persist current mesh before native work, as with import stages.
            if self.journal:
                self.journal()
            else:
                self.write_report()
            if self.stop_requested:
                return
            try:
                result = self.backend.bind_mesh(item, mesh_id)
                item.checked_slots += result['checked']
                item.repaired_slots += result['repaired']
                item.unassigned_slots += result['unassigned']
                item.binding_issues.extend(result['issues'])
            except Exception as exc:
                item.binding_issues.append('网格 {}：{}'.format(mesh_id, exc))
            finally:
                try:
                    self.backend.release_source_cache(item.target)
                    self.backend.cleanup()
                except Exception as exc:
                    item.binding_issues.append('绑定后清理失败：' + str(exc))
            item.binding_index += 1
            item.message = '材质恢复校验 {}/{}；已修复 {} 个槽；异常 {} 项。'.format(
                item.binding_index, len(item.binding_meshes), item.repaired_slots, len(item.binding_issues))
            self._memory_check(item, after=True)
            return
        # A second read-only pass catches conflicting aliases or bindings changed by later meshes.
        if not item.binding_issues and item.verification_index < len(item.binding_meshes):
            mesh_id = item.binding_meshes[item.verification_index]
            try:
                result = self.backend.check_mesh(item, mesh_id)
                item.binding_issues.extend(result['issues'])
            except Exception as exc:
                item.binding_issues.append('最终复核网格 {}：{}'.format(mesh_id, exc))
            finally:
                try:
                    self.backend.release_source_cache(item.target)
                    self.backend.cleanup()
                except Exception as exc:
                    item.binding_issues.append('复核后清理失败：' + str(exc))
            item.verification_index += 1
            item.message = '最终材质复核 {}/{}。'.format(item.verification_index, len(item.binding_meshes))
            self._memory_check(item, after=True)
            return
        item.validation_completed = True
        item.stage, item.status, self.phase = 'validation', 'compiling', 'compile'

    def _memory_check(self, item, after=False):
        try:
            self.memory = dict(self.backend.memory_status())
            physical, virtual = self.memory['available_physical_gb'], self.memory['available_virtual_gb']
            self.warning = ("低内存预警：可用物理内存 {:.2f} GB，可用虚拟内存 {:.2f} GB；仍继续导入。".format(physical, virtual)
                            if physical < self.min_physical_gb or virtual < self.min_virtual_gb else "")
        except Exception as exc:
            self.warning = "无法读取内存状态：{}；仍继续导入。".format(exc)
        if after:
            item.memory_after = dict(self.memory)
        else:
            item.memory_before = dict(self.memory)
        if self.warning:
            item.memory_warning = self.warning

    def _cancel_remaining(self):
        self.cancelled = True
        for item in self.items[self.index:]:
            if item.status == "waiting":
                item.status, item.message = "cancelled", "已取消，文件未导入。"
            elif item.status in ('waiting_materials', 'waiting_geometry', 'validating'):
                item.status, item.message = 'incomplete', '已取消；保留已导入资产，材质绑定校验尚未完成。'
                item.finished = now()
        self._finish()

    def remaining_seconds(self):
        return max(0.0, self.deadline - self.clock()) if self.phase == "cooldown" else 0.0

    def _finish(self):
        self.done, self.finished = True, now()

    def write_report(self):
        report = {
            "version": 3, "mode": "assets_only", "import_mode": self.mode,
            "started": self.started, "finished": self.finished,
            "output": self.root, "map": self.map_path, "cancelled": self.cancelled,
            "done": self.done, "phase": self.phase, "counts": dict(Counter(p.status for p in self.items)),
            "memory": self.memory, "memory_warning": self.warning, "low_memory_action": "warn_and_continue",
            "min_physical_gb": self.min_physical_gb, "min_virtual_gb": self.min_virtual_gb,
            "options": str(self.settings.get_editor_property("base_options")) if hasattr(self.settings, "get_editor_property") else str(self.settings),
            "files": [asdict(item) for item in self.items],
        }
        path = storage.write_json(self.report_name + ".json", report)
        stream = io.StringIO(newline="")
        keys = list(asdict(self.items[0])) if self.items else list(ImportItem.__dataclass_fields__)
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(asdict(item) for item in self.items)
        storage.write_text(self.report_name + ".csv", "\ufeff" + stream.getvalue())
        return path
