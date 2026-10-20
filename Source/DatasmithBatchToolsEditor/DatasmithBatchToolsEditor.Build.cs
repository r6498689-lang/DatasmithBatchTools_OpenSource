using UnrealBuildTool;

public class DatasmithBatchToolsEditor : ModuleRules
{
    public DatasmithBatchToolsEditor(ReadOnlyTargetRules Target) : base(Target)
    {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PublicDependencyModuleNames.AddRange(new string[] {
            "Core", "CoreUObject", "Engine", "GeometryCore", "MeshDescription", "StaticMeshDescription"
        });
        PrivateDependencyModuleNames.AddRange(new string[] {
            "UnrealEd", "DatasmithImporter", "DatasmithContent", "DatasmithCore",
            "DatasmithTranslator", "ExternalSource", "AssetRegistry",
            "EditorScriptingUtilities", "RenderCore"
        });
    }
}
