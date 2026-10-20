# -*- coding: utf-8 -*-
"""Validate the parent-material contract required for batch instance creation."""
from __future__ import annotations

API_VERSION = "1.0"


def _nodes(material):
    import unreal
    return [node for node in unreal.ObjectIterator(unreal.MaterialExpression) if node.get_outer() == material]


def _parameter(node):
    try:
        return str(node.get_editor_property("parameter_name"))
    except Exception:
        return ""


def _texture_connections(material, nodes, name):
    import unreal
    mel = unreal.MaterialEditingLibrary
    textures = [n for n in nodes if isinstance(n, unreal.MaterialExpressionTextureSampleParameter2D) and _parameter(n) == name]
    if len(textures) != 1:
        raise ValueError("母材质需要唯一的纹理参数节点：" + name)
    texture = textures[0]
    multiplies = [n for n in nodes if isinstance(n, unreal.MaterialExpressionMultiply) and texture in mel.get_inputs_for_material_expression(material, n)]
    if len(multiplies) != 1:
        raise ValueError("无法确认纹理参数与强度乘法节点的唯一连接：" + name)
    return texture, multiplies[0]


def verify_contract(material):
    import unreal
    from .material_instance_processor import SWITCHES, FALLBACKS, SRGB_SWITCHES
    mel = unreal.MaterialEditingLibrary
    nodes = _nodes(material)
    for purpose, name, channel in (("metal", "Metal", "B"), ("rough", "Roughness", "G")):
        switches = [n for n in nodes if isinstance(n, unreal.MaterialExpressionStaticSwitchParameter) and _parameter(n) == SRGB_SWITCHES[purpose]]
        if len(switches) != 1:
            raise ValueError("母材质缺少唯一的数据采样开关：" + name)
        switch = switches[0]
        sources = list(mel.get_inputs_for_material_expression(material, switch))
        if len(sources) != 2:
            raise ValueError("数据采样开关的两个分支必须都连接：" + name)
        gamma, texture = sources
        if not isinstance(texture, unreal.MaterialExpressionTextureSampleParameter2D) or _parameter(texture) != name:
            raise ValueError("线性数据采样分支不符：" + name)
        actual = mel.get_input_node_output_name_for_material_expression(switch, texture)
        if actual != channel:
            raise ValueError("母材质 {} 必须读取 {} 通道，实际为 {}".format(name, channel, actual))
        if texture.get_editor_property("sampler_type") != unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR:
            raise ValueError("母材质 {} 需要 Linear Color 数据采样。".format(name))
        convert = gamma
        if not isinstance(convert, unreal.MaterialExpressionMaterialFunctionCall):
            raise ValueError("sRGB 数据分支缺少转换函数：" + name)
        function = convert.get_editor_property("material_function")
        if not function or function.get_path_name().split(".")[0] != "/Engine/Functions/Engine_MaterialFunctions01/ImageAdjustment/LinearTosRGB":
            raise ValueError("sRGB 数据分支转换函数不符：" + name)
        convert_inputs = list(mel.get_inputs_for_material_expression(material, convert))
        gamma_sample = convert_inputs[0] if convert_inputs else None
        if not isinstance(gamma_sample, unreal.MaterialExpressionTextureSampleParameter2D) or _parameter(gamma_sample) != name or gamma_sample.get_editor_property("sampler_type") != unreal.MaterialSamplerType.SAMPLERTYPE_COLOR:
            raise ValueError("sRGB 数据分支纹理采样不符：" + name)
        if mel.get_input_node_output_name_for_material_expression(convert, gamma_sample) != channel:
            raise ValueError("sRGB 分支必须先提取指定通道再转换：" + name)
    for purpose, prop in (("metal", unreal.MaterialProperty.MP_METALLIC), ("rough", unreal.MaterialProperty.MP_ROUGHNESS), ("normal", unreal.MaterialProperty.MP_NORMAL)):
        root = mel.get_material_property_input_node(material, prop)
        if not isinstance(root, unreal.MaterialExpressionStaticSwitchParameter) or _parameter(root) != SWITCHES[purpose]:
            raise ValueError("母材质输出缺少贴图/备用值开关：" + SWITCHES[purpose])
        inputs = list(mel.get_inputs_for_material_expression(material, root))
        if len(inputs) != 2 or inputs[0] is None or inputs[1] is None:
            raise ValueError("贴图和备用值分支必须连接：" + SWITCHES[purpose])
        if purpose == "normal":
            fallback = inputs[1]
            if not isinstance(fallback, unreal.MaterialExpressionConstant3Vector):
                raise ValueError("法线开关缺少平坦法线分支。")
            value = fallback.get_editor_property("constant")
            if (value.r, value.g, value.b) != (0.0, 0.0, 1.0):
                raise ValueError("平坦法线备用值必须为 (0, 0, 1)。")
        elif _parameter(inputs[1]) != FALLBACKS[purpose]:
            raise ValueError("缺少连接到输出开关的备用值：" + FALLBACKS[purpose])
    return True
