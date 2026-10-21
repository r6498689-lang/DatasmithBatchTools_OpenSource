# Datasmith 批处理工具

这是一个面向 Unreal Editor 的插件，提供以下三项批处理功能：

1. 根据统一的贴图命名规则批量生成材质实例。
2. 批量修改选中贴图，或选中模型所引用贴图的属性，并提供可恢复的修改记录。
3. 按队列批量导入 Datasmith 文件，支持导入顺序、保存处理以及普通/大文件模式。

插件适用于 Unreal Engine 5.6 和 Windows，使用 Unreal 的 Python Editor 集成与 PySide6。批量导入功能还需要启用 Datasmith Importer 插件。

## 安装

1. 将 `DatasmithBatchTools` 文件夹复制到 `<项目目录>/Plugins/`。
2. 在项目中启用 **Python Editor Script Plugin**、**Editor Scripting Utilities** 和 **Datasmith Importer**。插件描述文件会在可用时启用这些依赖。
3. 重新生成项目文件并编译 Editor 目标，或者打开项目并等待 Unreal 编译 Editor 模块。
4. 在 Unreal Editor 中打开 **Window → Datasmith Batch Tools**。

首次启动时，Unreal 可能会安装 `DatasmithBatchTools.uplugin` 中指定版本的 PySide6 依赖。如果提示重启，请重启编辑器。

## 批量生成材质实例：必须遵守的命名规则

批量生成材质实例依赖以下资源和文件夹命名规则。**不符合规则的资源会被标记为无效并跳过；工具不会自动重命名资源。** 扫描前，请先按规则重命名贴图并整理文件夹。

```text
<贴图根目录>/<类别>/<材质编号>_<类别>_UV_<方向>/<贴图角色>_<材质编号>_<贴图名称>_<文件编号>
```

- `贴图根目录`：Unreal 内容路径，例如 `/Game/...`。
- `类别`：由字母、数字和下划线组成，且必须与类别目录名称完全一致。
- `材质编号`：至少三位数字，必须同时出现在文件夹名和贴图名中。
- `方向`：可填写 `U_V`、`U`、`V`、`Unknown` 或 `None`。
- `贴图角色`：通常为 `BaseColor`、`BaseColorAO`、`Normal`、`Metallic`、`Roughness`、`PackedRGBA` 或 `ChannelM`。
- `贴图名称`：由字母、数字和下划线组成。
- `文件编号`：至少三位数字；当存在多个可选贴图时，工具会按此编号确定性地选择第一个匹配项。

示例：

```text
/Game/Textures/Asphalt/107_Asphalt_UV_Unknown/BaseColor_107_Bridge_Asphalt_01_001
```

每组材质至少需要一张 `BaseColor` 或 `BaseColorAO` 贴图。所选主材质必须实现本工具使用的参数约定，包括 `base_color`、`Normal`、`Metal` 和 `Roughness` 贴图参数，以及 `DMA_...` 开关和默认值。工具会在创建材质实例前检查这些参数。目标位置已有的资源不会被覆盖。

## 批量修改贴图属性

贴图页面针对 Unreal `/Game` 路径中的贴图资源。可以直接扫描选中的贴图，也可以收集选中模型所引用的贴图。修改会记录在操作日志中，页面可在满足保护条件时提供恢复操作。每张贴图的颜色修改都需要单独确认。源图像文件不会被修改。

## 批量导入 Datasmith

将 `.udatasmith` 文件添加到队列，设置 `/Game` 下的目标目录并选择导入模式。文件会按界面显示的顺序导入，并在每个文件导入后保存资源。大文件模式会分阶段处理几何体和材质，并尝试在最终保存前恢复材质绑定。此功能不会将导入的 Actor 放置到关卡中。

## 测试

`Tests` 目录包含命名与导入规划、贴图修改记录以及 Datasmith 导入状态处理的离线测试。测试使用 Unreal Editor 模拟对象，不会修改项目资源。在仓库根目录运行：

```powershell
python -m unittest discover -s Tests -v
```

## 许可证

本项目使用 MIT 许可证。详见 [LICENSE](LICENSE)。
