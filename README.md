# Datasmith Batch Tools

Unreal Editor plugin with three batch workflows:

1. **Generate material instances** from consistently named texture assets.
2. **Edit texture properties** for selected textures or textures used by selected models, with a reversible change journal.
3. **Import Datasmith files in batches**, with ordered imports, save handling, and normal/large-file modes.

The plugin targets Unreal Engine 5.6 and Windows. It uses Unreal's Python Editor integration and PySide6. The import workflow also uses the Datasmith importer plugin.

## Install

1. Copy this `DatasmithBatchTools` folder into `<Project>/Plugins/`.
2. Enable **Python Editor Script Plugin**, **Editor Scripting Utilities**, and **Datasmith Importer** in the project. The plugin descriptor enables these dependencies when available.
3. Regenerate project files and build the editor target, or open the project and let Unreal compile the editor module.
4. In Unreal Editor, open **Window → Datasmith Batch Tools**.

On first launch, Unreal may install the pinned PySide6 requirements listed in `DatasmithBatchTools.uplugin`. Restart the editor after installation if prompted.

## Generate material instances: required naming convention

Material-instance generation depends on the asset and folder naming convention below. **Assets that do not follow it are reported as invalid and skipped; this tool does not rename assets for you.** Rename the texture assets and arrange their folders before scanning.

```text
<TextureRoot>/<Category>/<MaterialID>_<Category>_UV_<Direction>/<Role>_<MaterialID>_<TextureName>_<FileID>
```

- `TextureRoot`: an Unreal `/Game/...` content path.
- `Category`: letters, digits, and underscores; it must exactly match the category directory.
- `MaterialID`: at least three digits. It must match in the folder and filename.
- `Direction`: `U_V`, `U`, `V`, `Unknown`, or `None`.
- `Role`: typically `BaseColor`, `BaseColorAO`, `Normal`, `Metallic`, `Roughness`, `PackedRGBA`, or `ChannelM`.
- `TextureName`: letters, digits, and underscores.
- `FileID`: at least three digits; used to choose the first matching optional map deterministically.

Example:

```text
/Game/Textures/Asphalt/107_Asphalt_UV_Unknown/BaseColor_107_Bridge_Asphalt_01_001
```

Each material group must contain at least one `BaseColor` or `BaseColorAO` texture. The chosen master material must implement the parameter contract used by this tool, including `base_color`, `Normal`, `Metal`, and `Roughness` texture parameters and the `DMA_...` switches/defaults. The scan checks that contract before creating instances. Existing destination assets are not overwritten.

## Texture property editing

The texture page operates on Unreal `/Game` texture assets. It can scan selected textures directly or collect textures referenced by selected models. Changes are journaled so the page can offer guarded recovery. Color changes require explicit confirmation per texture. The source image files are not modified.

## Datasmith batch import

Add `.udatasmith` files to the queue, set the destination under `/Game`, and choose the import mode. Imports run in the displayed order and save assets between files. Large-file mode processes geometry and materials in stages and attempts to restore material bindings before the final save. It does not place imported actors into the level.

## Tests

The `Tests` directory contains offline tests for naming/planning, texture change journaling, and Datasmith import state handling. Tests use Unreal Editor stubs and do not modify project assets. Run from the repository root with:

```powershell
python -m unittest discover -s Tests -v
```

## License

MIT. See [LICENSE](LICENSE).
