# -*- coding: utf-8 -*-
"""Deterministic instance planning and editor-only, non-overwriting creation."""
from __future__ import annotations

import csv
import io
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Iterable
from uuid import uuid4

from . import storage

API_VERSION = "1.0.2"
DEFAULT_SOURCE = "/Game/Textures"
DEFAULT_PARENT = "/Game/Materials/M_Master"
DEFAULT_OUTPUT = "/Game/Materials/GeneratedInstances"
BASE_ROLES = {"BaseColor", "BaseColorAO"}
METAL_ROLES = {"Metallic", "PackedRGBA", "ChannelM"}
ROUGH_ROLES = {"Roughness", "PackedRGBA", "ChannelM"}
SUPPORTED = BASE_ROLES | METAL_ROLES | ROUGH_ROLES | {"Normal"}
TEXTURES = {"base": "base_color", "normal": "Normal", "metal": "Metal", "rough": "Roughness"}
SWITCHES = {"normal": "DMA_UseNormal", "metal": "DMA_UseMetal", "rough": "DMA_UseRoughness"}
SRGB_SWITCHES = {"metal": "DMA_MetalSRGB", "rough": "DMA_RoughnessSRGB"}
FALLBACKS = {"metal": "DMA_DefaultMetal", "rough": "DMA_DefaultRoughness"}
STATUS = {"ready": "待创建", "created": "已创建", "existing": "已有跳过", "invalid": "无效", "conflict": "冲突跳过", "failed": "失败", "cancelled": "未执行（已取消）"}
NAME_RE = re.compile(r"^(?P<role>[A-Za-z]+)_(?P<id>\d{3,})_(?P<name>[A-Za-z0-9_]+)_(?P<file>\d{3,})$")
FOLDER_RE = re.compile(r"^(?P<id>\d{3,})_(?P<category>[A-Za-z0-9_]+?)_UV_(?:U_V|U|V|Unknown|None)$")


def ue():
    import unreal
    return unreal


def package_path(value: str) -> str:
    value = str(value).strip().replace("\\", "/")
    if "'" in value:
        value = value.split("'", 2)[1]
    return value.rstrip("/").split(".", 1)[0]


def game_directory(value: str) -> str:
    original = str(value)
    value = original.strip().replace("\\", "/").rstrip("/")
    # UE5's Content Browser returns virtual paths such as /All/Game
    # from both folder-selection APIs. Asset APIs require the internal /Game path.
    # Strip only this exact project mount, never /All/Engine or plugin roots.
    if value == "/All/Game" or value.startswith("/All/Game/"):
        value = value[len("/All"):]
    if not re.fullmatch(r"/Game(?:/[A-Za-z0-9_]+)*", value):
        raise ValueError("请输入 /Game 下的 UE 目录，路径只使用英文字母、数字和下划线。\n收到的路径：" + original)
    return value


def options(source=DEFAULT_SOURCE, parent=DEFAULT_PARENT, output=DEFAULT_OUTPUT, metal=0.0, rough=0.5) -> dict:
    source, output = game_directory(source), game_directory(output)
    parent = package_path(parent)
    if not re.fullmatch(r"/Game(?:/[A-Za-z0-9_]+)+", parent):
        raise ValueError("母材质必须是 /Game 下的有效资产路径。")
    metal, rough = float(metal), float(rough)
    if not all(math.isfinite(x) and 0 <= x <= 1 for x in (metal, rough)):
        raise ValueError("缺图备用值必须在 0～1 之间。")
    return dict(source=source, parent=parent, output=output, metal=metal, rough=rough)


@dataclass(frozen=True)
class TextureRecord:
    path: str
    role: str
    material_id: str
    category: str
    stem: str
    file_id: int

    @property
    def order(self):
        return self.file_id, self.path.casefold(), self.path


@dataclass
class InstancePlan:
    material_id: str
    category: str
    base: str
    target: str
    normal: str = ""
    metal: str = ""
    rough: str = ""
    candidates: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    status: str = "ready"
    message: str = ""


def parse_texture(path: str, source: str) -> TextureRecord:
    path, source = package_path(path), game_directory(source)
    if not path.startswith(source + "/"):
        raise ValueError("资产不在指定扫描目录下")
    name = path.rsplit("/", 1)[-1]
    match = NAME_RE.fullmatch(name)
    if not match:
        raise ValueError("名称不符合 Role_MaterialID_TextureName_FileID（编号至少三位）")
    parts = path.split("/")
    folder = FOLDER_RE.fullmatch(parts[-2])
    if not folder or parts[-3] != folder["category"]:
        raise ValueError("分类与材质组目录不符合 Category/MaterialID_Category_UV_Direction")
    if folder["id"] != match["id"]:
        raise ValueError("目录与文件的材质编号不一致")
    return TextureRecord(path, match["role"], match["id"], folder["category"],
                         name[len(match["role"]) + 1:], int(match["file"]))


def build_plans(paths: Iterable[str], config: dict, exists: Callable[[str], str] | None = None) -> tuple[list, list, dict]:
    """Pure planner. exists returns 'instance', 'other', or an empty string."""
    groups, issues, plans = defaultdict(list), [], []
    count = 0
    for path in sorted({package_path(p) for p in paths}):
        count += 1
        try:
            record = parse_texture(path, config["source"])
            groups[record.material_id].append(record)
            if record.role not in SUPPORTED:
                issues.append(dict(path=path, reason="忽略未支持用途：" + record.role))
        except ValueError as exc:
            issues.append(dict(path=path, reason=str(exc)))
    for material_id, records in sorted(groups.items()):
        categories = {r.category for r in records}
        bases = [r for r in records if r.role in BASE_ROLES]
        if len(categories) != 1:
            issues.append(dict(path=material_id, reason="同一材质编号出现在不同分类中"))
            for base in bases:
                plans.append(InstancePlan(material_id, base.category, base.path, "", status="invalid", message="材质编号分类冲突"))
            continue
        if not bases:
            issues.append(dict(path=material_id, reason="没有 BaseColor 或 BaseColorAO，跳过该组"))
            continue
        candidates = {
            "normal": sorted((r for r in records if r.role == "Normal"), key=lambda r: r.order),
            "metal": sorted((r for r in records if r.role in METAL_ROLES), key=lambda r: r.order),
            "rough": sorted((r for r in records if r.role in ROUGH_ROLES), key=lambda r: r.order),
        }
        for base in sorted(bases, key=lambda r: r.order):
            target = f"{config['output']}/{base.category}/{material_id}_{base.category}/{base.stem}"
            plan = InstancePlan(material_id, base.category, base.path, target)
            for purpose, values in candidates.items():
                plan.candidates[purpose] = [v.path for v in values]
                if values:
                    setattr(plan, purpose, values[0].path)
                    if len(values) > 1:
                        plan.notes.append(f"{TEXTURES[purpose]}：{len(values)} 个候选，按 FileID/路径取第一张")
                else:
                    fallback = "平坦法线" if purpose == "normal" else str(config[purpose])
                    plan.notes.append(f"{TEXTURES[purpose]} 缺图，使用 {fallback}")
            if base.role == "BaseColorAO":
                plan.notes.append("BaseColorAO 按 RGB 基础色使用，不单独提取 AO")
            existing = exists(target) if exists else ""
            if existing:
                plan.status = "existing" if existing == "instance" else "conflict"
                plan.message = "已有同名实例，保留现有参数" if existing == "instance" else "目标已有其他类型资产"
            plans.append(plan)
    counts = Counter(p.target.casefold() for p in plans if p.target)
    for plan in plans:
        if plan.target and counts[plan.target.casefold()] > 1:
            plan.status, plan.message = "conflict", "多个基础色去掉前缀后指向同一实例路径"
    summary = dict(textures=count, groups=len(groups), plans=len(plans), issues=len(issues))
    summary.update(Counter(p.status for p in plans))
    return plans, issues, summary


def existing_kind(path: str) -> str:
    u = ue()
    if not u.EditorAssetLibrary.does_asset_exist(path):
        return ""
    data = u.EditorAssetLibrary.find_asset_data(path)
    name = str(data.asset_class_path.asset_name)
    return "instance" if name == "MaterialInstanceConstant" else "other"


def scan_textures(config: dict):
    """Yield progress chunks without loading texture objects or writing assets."""
    u = ue()
    registry = u.AssetRegistryHelpers.get_asset_registry()
    if not u.EditorAssetLibrary.does_directory_exist(config["source"]):
        raise ValueError("贴图根目录不存在：" + config["source"])
    registry.scan_paths_synchronous([config["source"]], force_rescan=False)
    assets = registry.get_assets_by_path(config["source"], recursive=True)
    paths = []
    total = len(assets)
    for i, data in enumerate(assets, 1):
        if str(data.asset_class_path.asset_name) == "Texture2D":
            paths.append(str(data.package_name))
        if i % 64 == 0:
            yield dict(done=i, total=total)
    yield dict(done=total, total=total)
    plans, issues, summary = build_plans(paths, config, existing_kind)
    yield dict(done=total, total=total, plans=plans, issues=issues, summary=summary)


def validate_parent(path: str):
    u = ue()
    parent = u.EditorAssetLibrary.load_asset(path)
    if not isinstance(parent, u.Material):
        raise ValueError("请选择已适配的母材质（Material），不能选择材质实例。")
    mel = u.MaterialEditingLibrary
    checks = [
        (mel.get_texture_parameter_names, set(TEXTURES.values()), "纹理参数"),
        (mel.get_static_switch_parameter_names, set(SWITCHES.values()) | set(SRGB_SWITCHES.values()), "贴图控制开关"),
        (mel.get_scalar_parameter_names, set(FALLBACKS.values()), "备用值参数"),
    ]
    for get_names, required, label in checks:
        missing = required - {str(x) for x in get_names(parent)}
        if missing:
            raise ValueError("母材质尚未适配，缺少{}：{}".format(label, "、".join(sorted(missing))))
    # Contract validation checks actual texture-to-multiply connections, not just names.
    from .material_instance_master import verify_contract
    verify_contract(parent)
    return parent


def create_one(plan: InstancePlan, config: dict, parent=None) -> InstancePlan:
    u = ue()
    if plan.status != "ready":
        return plan
    try:
        parent = parent or validate_parent(config["parent"])
        existing = existing_kind(plan.target)
        if existing:
            plan.status = "existing" if existing == "instance" else "conflict"
            plan.message = "执行前发现同名资产，未覆盖"
            return plan
        textures = {}
        for purpose in TEXTURES:
            path = getattr(plan, purpose)
            if path:
                asset = u.EditorAssetLibrary.load_asset(path)
                if not isinstance(asset, u.Texture2D):
                    raise ValueError("贴图已删除或类型不符：" + path)
                if bool(asset.get_editor_property("virtual_texture_streaming")):
                    raise ValueError("当前母材质不支持虚拟纹理采样：" + path)
                textures[purpose] = asset
        _check_texture_settings(textures)
        folder, name = plan.target.rsplit("/", 1)
        if not u.EditorAssetLibrary.does_directory_exist(folder):
            if not u.EditorAssetLibrary.make_directory(folder):
                raise RuntimeError("无法创建输出目录：" + folder)
        instance = u.AssetToolsHelpers.get_asset_tools().create_asset(name, folder, u.MaterialInstanceConstant, u.MaterialInstanceConstantFactoryNew())
        if instance is None:
            raise RuntimeError("创建实例失败，可能目标已被占用")
        mel = u.MaterialEditingLibrary
        mel.set_material_instance_parent(instance, parent)
        # UE 5.6's MaterialEditingLibrary setters return false even when the
        # assignment succeeds (see engine source). Verify read-back instead.
        for purpose, asset in textures.items():
            mel.set_material_instance_texture_parameter_value(instance, TEXTURES[purpose], asset)
        for purpose, name in SWITCHES.items():
            mel.set_material_instance_static_switch_parameter_value(instance, name, purpose in textures, update_material_instance=False)
        for purpose, name in SRGB_SWITCHES.items():
            srgb = bool(textures[purpose].get_editor_property("srgb")) if purpose in textures else False
            mel.set_material_instance_static_switch_parameter_value(instance, name, srgb, update_material_instance=False)
        for purpose, name in FALLBACKS.items():
            mel.set_material_instance_scalar_parameter_value(instance, name, config[purpose])
        mel.update_material_instance(instance)
        for purpose, asset in textures.items():
            actual = mel.get_material_instance_texture_parameter_value(instance, TEXTURES[purpose])
            if actual != asset:
                raise RuntimeError("保存后纹理参数核对失败：" + TEXTURES[purpose])
        for purpose, name in SWITCHES.items():
            if mel.get_material_instance_static_switch_parameter_value(instance, name) != (purpose in textures):
                raise RuntimeError("保存后缺图开关核对失败：" + name)
        for purpose, name in SRGB_SWITCHES.items():
            expected = bool(textures[purpose].get_editor_property("srgb")) if purpose in textures else False
            if mel.get_material_instance_static_switch_parameter_value(instance, name) != expected:
                raise RuntimeError("数据采样开关核对失败：" + name)
        for purpose, name in FALLBACKS.items():
            if not math.isclose(mel.get_material_instance_scalar_parameter_value(instance, name), config[purpose], abs_tol=1e-6):
                raise RuntimeError("备用值核对失败：" + name)
        if not u.EditorAssetLibrary.save_loaded_asset(instance, only_if_is_dirty=False):
            raise RuntimeError("实例已创建但保存失败，请检查只读或源控制状态：" + plan.target)
        plan.status, plan.message = "created", "已创建并保存"
    except Exception as exc:
        plan.status, plan.message = "failed", str(exc)
        if u.EditorAssetLibrary.does_asset_exist(plan.target):
            plan.message += "；目标资产可能已创建，请检查后再重试"
    return plan


def _check_texture_settings(textures: dict):
    """Never silently sample data as gamma-encoded color or alter shared textures."""
    u = ue()
    for purpose, asset in textures.items():
        srgb = bool(asset.get_editor_property("srgb"))
        compression = asset.get_editor_property("compression_settings")
        specialized = ("TC_GRAYSCALE", "TC_ALPHA", "TC_MASKS", "TC_DISTANCE_FIELD_FONT")
        if any(name in str(compression).upper() for name in specialized):
            raise ValueError("贴图压缩需要专用采样器，当前母材质不兼容（原贴图未修改）：" + asset.get_path_name())
        if purpose == "base" and (not srgb or compression == u.TextureCompressionSettings.TC_NORMALMAP):
            raise ValueError("基础色需启用 sRGB 并使用颜色采样兼容压缩，请先检查：" + asset.get_path_name())
        if purpose in ("metal", "rough") and compression == u.TextureCompressionSettings.TC_NORMALMAP:
            raise ValueError("金属度/粗糙度不能使用丢弃原始 B 通道的法线压缩：" + asset.get_path_name())
        if purpose == "normal" and (srgb or compression != u.TextureCompressionSettings.TC_NORMALMAP):
            raise ValueError("法线需关闭 sRGB 并使用 Normalmap 压缩，请先检查：" + asset.get_path_name())


def write_report(batch: dict) -> str:
    stem = batch["stem"]
    payload = {k: v for k, v in batch.items() if k != "plans"}
    payload["rows"] = [asdict(p) for p in batch["plans"]]
    payload["summary"] = dict(Counter(p.status for p in batch["plans"]))
    result = storage.write_json(stem + ".json", payload)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["MaterialID", "Category", "BaseColor", "Normal", "Metal_B", "Roughness_G", "Target", "Status", "Message", "Notes"])
    for p in batch["plans"]:
        writer.writerow([p.material_id, p.category, p.base, p.normal, p.metal, p.rough, p.target, STATUS[p.status], p.message, "；".join(p.notes)])
    storage.write_text(stem + ".csv", "\ufeff" + output.getvalue())
    return result


def begin_batch(config: dict, plans: list, issues: list) -> dict:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    batch = dict(schema=1, started=datetime.now().isoformat(timespec="seconds"),
                 stem="material_instance_batches/instances_" + stamp,
                 config=dict(config), plans=plans, issues=issues, state="running")
    write_report(batch)
    return batch


def finish_batch(batch: dict, cancelled=False, failed=False) -> str:
    batch["state"] = "failed" if failed else "cancelled" if cancelled else "completed"
    batch["finished"] = datetime.now().isoformat(timespec="seconds")
    if cancelled or failed:
        for p in batch["plans"]:
            if p.status == "ready":
                p.status, p.message = "cancelled", "当前批次已停止，尚未执行"
    return write_report(batch)
