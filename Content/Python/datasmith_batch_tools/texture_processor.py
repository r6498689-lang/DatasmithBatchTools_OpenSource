# -*- coding: utf-8 -*-
"""Scoped Texture2D batches with durable write-ahead parameter recovery.

Only UE asset properties are written. No image files, compression settings,
texture groups, sRGB flags or mip-generation algorithms are changed.
"""
from __future__ import annotations

import csv
import io
import math
import os
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import unreal
from . import storage

API_VERSION = "1.0"
SCHEMA_VERSION = 1
HISTORY_DIR = "texture_batches"
COLOR_FIELDS = {
    "adjust_brightness": ("亮度", 1.0, 0.0, 10.0),
    "adjust_hue": ("色相偏移", 0.0, 0.0, 360.0),
    "adjust_saturation": ("饱和度", 1.0, 0.0, 10.0),
    "adjust_vibrance": ("自然饱和度", 0.0, 0.0, 1.0),
}
PROPERTY_FIELDS = ("max_texture_size", "lod_bias", *COLOR_FIELDS)
DATA_NAME = re.compile(
    r"(?:^|[_\W])(normal|normals|nrm|nor|orm|arm|rma|mask|masks|roughness|"
    r"metallic|metalness|occlusion|ao|height|displacement|opacity)(?:$|[_\W])|法线|粗糙|金属|遮罩|高度",
    re.I,
)
BASE_NAME = re.compile(r"base[ _-]?color|albedo|diffuse|(?:^|[_\W])(bc|basecolour)(?:$|[_\W])|基础色|漫反射", re.I)
DATA_COMPRESSION = (
    "NORMALMAP", "MASKS", "GRAYSCALE", "DISPLACEMENT", "ALPHA", "HDR",
    "DISTANCE_FIELD", "BC4", "SINGLE_FLOAT", "HALF_FLOAT",
)


@dataclass
class TextureRecord:
    path: str
    name: str
    source_width: int
    source_height: int
    source_identity: str
    has_mips: bool
    virtual: bool
    srgb: bool
    compression: str
    group: str
    values: dict
    kind: str
    reason: str
    materials: list = field(default_factory=list)
    hints: list = field(default_factory=list)
    editable: bool = True
    status: str = "未处理"


def native_library():
    library = getattr(unreal, "DatasmithTextureBatchLibrary", None)
    if library is None:
        raise RuntimeError("贴图原生接口未加载。请安装本次 UE5.6 编译的 DLL 并重启 UE。")
    return library


def _value(asset, name, default=None):
    try:
        return asset.get_editor_property(name)
    except Exception:
        return default


def _snapshot(asset):
    # Required properties must all be readable; never invent rollback values.
    return {key: asset.get_editor_property(key) for key in PROPERTY_FIELDS}


def classify(name, hints, compression, srgb):
    evidence = " ".join([name, *hints])
    if any(token in compression.upper() for token in DATA_COMPRESSION) or DATA_NAME.search(evidence):
        return "data", "数据/HDR 贴图：禁用调色，可单独降分辨率"
    if BASE_NAME.search(evidence) and srgb:
        return "basecolor", "名称/参数提示为 BaseColor 候选，仍需人工勾选确认"
    return "unknown", "用途不确定{}：确认用于颜色后才勾选调色".format("（非 sRGB）" if not srgb else "")


def inspect_texture(asset, materials=(), hints=()):
    if not isinstance(asset, unreal.Texture2D):
        raise TypeError("仅支持 Texture2D")
    library = native_library()
    point = library.get_texture_source_size(asset)
    width, height = int(point.x), int(point.y)
    path = asset.get_path_name()
    name = asset.get_name()
    compression = str(_value(asset, "compression_settings", ""))
    srgb = bool(_value(asset, "srgb", False))
    kind, reason = classify(name, hints, compression, srgb)
    editable = width > 0 and height > 0 and path.startswith("/Game/")
    if not width or not height:
        reason = "缺少可用源图数据，跳过修改"
    elif not path.startswith("/Game/"):
        reason = "非 /Game 项目资产，首版不修改引擎或插件公共资产"
    return TextureRecord(
        path, name, width, height, str(library.get_texture_source_identity(asset)),
        bool(library.texture_has_effective_mips(asset)),
        bool(_value(asset, "virtual_texture_streaming", False)), srgb, compression,
        str(_value(asset, "lod_group", "")), _snapshot(asset), kind, reason,
        sorted(set(materials)), sorted(set(hints)), editable,
    )


def scan_items(source, selected_assets=None, selected_actors=None):
    """Yield lightweight collection steps, then one inspection step per asset.

    Kept as a generator for the UI Slate queue, not a background UObject thread.
    """
    collected = {}
    library = native_library()
    if source == "selected_textures":
        for asset in (unreal.EditorUtilityLibrary.get_selected_assets() if selected_assets is None else selected_assets):
            if isinstance(asset, unreal.Texture2D):
                collected.setdefault(asset.get_path_name(), {"materials": set(), "hints": set()})
            yield {"message": "读取内容浏览器选择"}
    elif source == "selected_models":
        subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        stack = list(subsystem.get_selected_level_actors() if selected_actors is None else selected_actors)
        visited, seen_materials = set(), set()
        while stack:
            actor = stack.pop()
            path = actor.get_path_name()
            if path in visited:
                continue
            visited.add(path)
            stack.extend(actor.get_attached_actors())
            child_component_class = getattr(unreal, "ChildActorComponent", None)
            if child_component_class:
                for child_component in actor.get_components_by_class(child_component_class):
                    child = _value(child_component, "child_actor")
                    if child:
                        stack.append(child)
            for component in actor.get_components_by_class(unreal.MeshComponent):
                for slot in range(component.get_num_materials()):
                    material = component.get_material(slot)
                    if not material:
                        continue
                    material_path = material.get_path_name()
                    if material_path in seen_materials:
                        continue
                    seen_materials.add(material_path)
                    hints_by_path = {}
                    try:
                        textures = library.get_material_texture_assets(material)
                        if isinstance(material, unreal.MaterialInstanceConstant):
                            for parameter in unreal.MaterialEditingLibrary.get_texture_parameter_names(material):
                                texture = unreal.MaterialEditingLibrary.get_material_instance_texture_parameter_value(material, parameter)
                                if texture:
                                    hints_by_path.setdefault(texture.get_path_name(), set()).add(str(parameter))
                    except Exception as exc:
                        yield {"error": "材质 {} 无法收集，已跳过：{}".format(material_path, exc)}
                        continue
                    if not textures:
                        yield {"warning": "材质 {} 未解析出 Texture2D；若仍在编译或存在编译错误，请完成后重新扫描。".format(material_path)}
                    for texture in textures:
                        texture_path = texture.get_path_name()
                        item = collected.setdefault(texture_path, {"materials": set(), "hints": set()})
                        item["materials"].add(material_path)
                        item["hints"].update(hints_by_path.get(texture_path, ()))
                    yield {"message": "收集材质：{}".format(material.get_name())}
            yield {"message": "收集模型：{}".format(actor.get_actor_label())}
    else:
        raise ValueError("未知扫描来源")
    # No UObject references are retained in result records.
    total = len(collected)
    for index, path in enumerate(sorted(collected)):
        try:
            asset = unreal.EditorAssetLibrary.load_asset(path)
            if asset is None:
                raise RuntimeError("资产无法加载")
            item = collected[path]
            record = inspect_texture(asset, item["materials"], item["hints"])
            yield {"record": record, "done": index + 1, "total": total, "message": record.name}
        except Exception as exc:
            yield {"error": "{}：{}".format(path, exc), "done": index + 1, "total": total}


def validate_options(options):
    mode = options.get("resolution_mode", "max_size")
    if mode not in ("max_size", "lod_bias"):
        raise ValueError("未知降分辨率模式")
    if options.get("resolution_enabled"):
        key = "max_texture_size" if mode == "max_size" else "lod_bias"
        value = options.get(key, 2048 if mode == "max_size" else 1)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value:
            raise ValueError("尺寸和 LOD Bias 必须为整数")
        if mode == "max_size" and value not in (256, 512, 1024, 2048, 4096, 8192, 16384):
            raise ValueError("请选择提供的最大纹理尺寸")
        if mode == "lod_bias" and not 0 <= value <= 12:
            raise ValueError("LOD Bias 范围为 0～12")
    colors = options.get("colors", {})
    for key, value in colors.items():
        if key not in COLOR_FIELDS:
            raise ValueError("不支持的颜色参数：{}".format(key))
        _, _, minimum, maximum = COLOR_FIELDS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError("{} 数值无效".format(COLOR_FIELDS[key][0]))
    if not options.get("resolution_enabled") and not colors:
        raise ValueError("请至少启用一项降分辨率或颜色参数")
    return options


def values_equal(a, b):
    return set(a) == set(b) and all(
        math.isclose(float(a[key]), float(b[key]), rel_tol=1e-6, abs_tol=1e-5)
        for key in a
    )


def plan_record(record, options, color_confirmed=False):
    validate_options(options)
    warnings, changes = [], {}
    if not record.editable:
        return {"before": {}, "after": {}, "warnings": [record.reason]}
    if options.get("resolution_enabled"):
        if options["resolution_mode"] == "max_size":
            changes.update(max_texture_size=int(options["max_texture_size"]), lod_bias=0)
        elif record.has_mips:
            changes.update(lod_bias=int(options["lod_bias"]), max_texture_size=0)
        else:
            warnings.append("无有效 mip：跳过 LOD Bias，不自动改变 mip 算法")
    if options.get("colors"):
        if record.kind == "data":
            warnings.append("数据/HDR 贴图禁止调色")
        elif color_confirmed:
            changes.update(options["colors"])
        else:
            warnings.append("未勾选确认用于颜色：跳过调色")
    after = {key: value for key, value in changes.items() if not values_equal({key: record.values[key]}, {key: value})}
    return {"before": {key: record.values[key] for key in after}, "after": after, "warnings": warnings}


def estimated_size(record, after):
    """Estimate from source and asset-level controls only, not an RHI guarantee."""
    width, height = record.source_width, record.source_height
    settings = dict(record.values, **after)
    maximum = int(settings["max_texture_size"])
    while maximum > 0 and max(width, height) > maximum:
        width, height = max(1, width // 2), max(1, height // 2)
    if record.has_mips:
        bias = max(0, int(settings["lod_bias"]))
        width, height = max(1, width >> bias), max(1, height >> bias)
    return width, height


def _history_name(batch_id):
    if not re.fullmatch(r"\d{8}_\d{6}_\d{6}_[0-9a-f]{8}", str(batch_id)):
        raise ValueError("无效批次编号")
    return os.path.join(HISTORY_DIR, str(batch_id) + ".json")


def write_batch(batch):
    return storage.write_json(_history_name(batch["batch_id"]), batch)


def new_batch(options, mode="apply", source_batch=""):
    batch = {
        "schema": SCHEMA_VERSION,
        "batch_id": datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:8],
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode, "source_batch": source_batch, "options": options,
        "status": "running", "entries": [],
    }
    write_batch(batch)  # Must succeed BEFORE any asset mutation.
    return batch


def list_history():
    candidates = {}
    for directory in storage.candidate_directories():
        history_dir = os.path.join(directory, HISTORY_DIR)
        if not os.path.isdir(history_dir):
            continue
        for filename in os.listdir(history_dir):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(history_dir, filename)
            if filename not in candidates or os.path.getmtime(path) > os.path.getmtime(candidates[filename]):
                candidates[filename] = path
    result = []
    for filename in candidates:
        try:
            batch = storage.read_json(os.path.join(HISTORY_DIR, filename), {})
            if batch.get("schema") != SCHEMA_VERSION:
                continue
            result.append(batch)
        except Exception as exc:
            unreal.log_warning("[TextureBatch] 历史记录不可读：{}：{}".format(filename, exc))
    return sorted(result, key=lambda value: value.get("batch_id", ""), reverse=True)


def _perform_write(asset, values):
    error = native_library().apply_texture_properties(asset, values)
    if error:
        raise RuntimeError(str(error))
    actual = {key: asset.get_editor_property(key) for key in values}
    if not values_equal(actual, values):
        raise RuntimeError("UE 写入后参数与目标值不一致")
    if not unreal.EditorAssetLibrary.save_loaded_asset(asset, only_if_is_dirty=False):
        raise RuntimeError("资产保存失败（可能只读或未签出）")
    return actual


class RecoveryWriteError(RuntimeError):
    """A durable recovery journal failed; the queue MUST stop, not just skip."""


def process_one(batch, record, options, color_confirmed=False):
    entry = {"path": record.path, "name": record.name, "before": {}, "after": {}, "warnings": [], "status": "skipped", "message": ""}
    batch["entries"].append(entry)
    asset = None
    write_attempted = False
    try:
        asset = unreal.EditorAssetLibrary.load_asset(record.path)
        if not asset:
            raise RuntimeError("贴图不存在")
        current = inspect_texture(asset, record.materials, record.hints)
        if current.source_identity != record.source_identity or not values_equal(current.values, record.values):
            entry["message"] = "扫描后源图或参数已变化，请重新扫描"
        else:
            plan = plan_record(current, options, color_confirmed)
            entry.update(plan)
            if not plan["after"]:
                entry["message"] = "无可执行改动；参数已相同或已安全跳过"
            else:
                entry["source_identity"] = current.source_identity
                entry["status"] = "prepared"
                entry["message"] = "修改前参数已记录"
                try:
                    write_batch(batch)  # Durable write-ahead journal before native call.
                except Exception as exc:
                    raise RecoveryWriteError("恢复记录写入失败，已阻止修改：{}".format(exc)) from exc
                write_attempted = True
                actual = _perform_write(asset, plan["after"])
                entry["after"] = actual
                entry["status"] = "applied"
                entry["message"] = "已修改并保存"
    except RecoveryWriteError:
        raise
    except Exception as exc:
        entry["status"], entry["message"] = "failed", str(exc)
        if write_attempted and asset and entry["before"]:
            try:
                _perform_write(asset, entry["before"])
                entry["message"] += "；本张已回滚原参数并保存"
            except Exception as rollback_error:
                entry["status"] = "rollback_failed"
                entry["message"] += "；回滚失败：{}，请查看恢复记录".format(rollback_error)
    try:
        write_batch(batch)
    except Exception as exc:
        raise RecoveryWriteError("批次状态写入失败，已停止后续处理：{}".format(exc)) from exc
    return entry


def restore_one(batch, source_entry):
    entry = {"path": source_entry["path"], "name": source_entry.get("name", ""), "before": {}, "after": {}, "status": "skipped", "message": "", "warnings": []}
    batch["entries"].append(entry)
    asset = None
    write_attempted = False
    try:
        if source_entry.get("status") not in ("applied", "prepared", "restored", "rollback_failed"):
            entry["message"] = "该条没有成功修改或待恢复参数"
        elif not source_entry.get("before") or set(source_entry["before"]) != set(source_entry.get("after", {})):
            entry["message"] = "恢复字段不完整，跳过"
        elif not set(source_entry["before"]).issubset(PROPERTY_FIELDS):
            entry["message"] = "记录包含不支持的参数，跳过"
        else:
            asset = unreal.EditorAssetLibrary.load_asset(entry["path"])
            if not asset:
                raise RuntimeError("资产不存在或无法加载")
            current = inspect_texture(asset)
            expected = source_entry["after"]
            actual = {key: current.values[key] for key in expected}
            if not current.editable or current.source_identity != source_entry.get("source_identity"):
                entry["message"] = "源图已变化或不可修改；参数记录无法恢复源图"
            elif values_equal(actual, source_entry["before"]):
                entry["message"] = "已经恢复，无需重复操作"
            elif not values_equal(actual, expected):
                entry["message"] = "检测到相关参数后续改动，跳过以保护新修改"
            else:
                entry.update(before=actual, after=dict(source_entry["before"]), source_identity=current.source_identity, status="prepared", message="恢复前已记录")
                try:
                    write_batch(batch)
                except Exception as exc:
                    raise RecoveryWriteError("恢复记录写入失败，已阻止修改：{}".format(exc)) from exc
                write_attempted = True
                entry["after"] = _perform_write(asset, entry["after"])
                entry.update(status="restored", message="已恢复本批次改动参数并保存")
    except RecoveryWriteError:
        raise
    except Exception as exc:
        entry.update(status="failed", message=str(exc))
        if write_attempted and asset and entry["before"]:
            try:
                _perform_write(asset, entry["before"])
                entry["message"] += "；恢复失败，本张已退回恢复前参数"
            except Exception as rollback_error:
                entry.update(status="rollback_failed", message=entry["message"] + "；回滚失败：" + str(rollback_error))
    try:
        write_batch(batch)
    except Exception as exc:
        raise RecoveryWriteError("恢复状态写入失败，已停止后续处理：{}".format(exc)) from exc
    return entry


def finish_batch(batch, cancelled=False):
    batch["status"] = "cancelled" if cancelled else "finished"
    batch["finished_at"] = datetime.now().isoformat(timespec="seconds")
    batch["summary"] = dict(Counter(entry["status"] for entry in batch["entries"]))
    path = write_batch(batch)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("资产路径", "状态", "修改前", "修改后", "说明", "警告"))
    for entry in batch["entries"]:
        writer.writerow((entry["path"], entry["status"], str(entry["before"]), str(entry["after"]), entry["message"], "；".join(entry.get("warnings", []))))
    # JSON is the authoritative recovery record. CSV/log failure never invalidates it.
    try:
        stem = os.path.join(HISTORY_DIR, batch["batch_id"])
        storage.write_text(stem + ".csv", "\ufeff" + output.getvalue())
        storage.write_text(stem + ".log", "\n".join("{} [{}] {}".format(e["path"], e["status"], e["message"]) for e in batch["entries"]))
    except Exception as exc:
        unreal.log_warning("[TextureBatch] JSON 已保存，附加报告生成失败：{}".format(exc))
    return path
