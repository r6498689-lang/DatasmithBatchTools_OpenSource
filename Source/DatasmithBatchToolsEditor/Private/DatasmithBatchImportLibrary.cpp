#include "DatasmithBatchImportLibrary.h"

#include "AssetRegistry/AssetRegistryModule.h"
#include "AssetCompilingManager.h"
#include "HAL/PlatformMemory.h"
#include "RenderingThread.h"
#include "UObject/GarbageCollection.h"

#include "DatasmithImportContext.h"
#include "DatasmithImportFactory.h"
#include "DatasmithImporter.h"
#include "DatasmithImportOptions.h"
#include "DatasmithScene.h"
#include "DatasmithSceneFactory.h"
#include "ExternalSource.h"
#include "ExternalSourceModule.h"
#include "EditorDialogLibrary.h"
#include "HAL/FileManager.h"
#include "IDatasmithSceneElements.h"
#include "Misc/PackageName.h"
#include "Misc/Paths.h"
#include "Misc/ScopeExit.h"
#include "SourceUri.h"
#include "UObject/StrongObjectPtr.h"
#include "UObject/UnrealType.h"
#include "UObject/UObjectIterator.h"
#include "Serialization/BulkDataRegistry.h"
#include "Engine/Texture2D.h"
#include "Engine/StaticMesh.h"
#include "StaticMeshResources.h"
#include "Materials/MaterialInterface.h"
#include "ObjectTemplates/DatasmithStaticMeshTemplate.h"
#include "Utility/DatasmithImporterUtils.h"
#include "DatasmithUtils.h"
#include "DatasmithAssetImportData.h"
#include "DatasmithTranslator.h"
#include "DatasmithPayload.h"
#include "HAL/PlatformMisc.h"
#include "Misc/OutputDeviceRedirector.h"

int32 UDatasmithBatchImportLibrary::ReleaseSavedSourceCache(const FString& AssetRoot)
{
    check(IsInGameThread());
    if (!AssetRoot.StartsWith(TEXT("/Game/")) || !FPackageName::IsValidLongPackageName(AssetRoot)) { return 0; }
    int32 Count = 0;
    IBulkDataRegistry& Registry = IBulkDataRegistry::Get();
    for (TObjectIterator<UTexture2D> It; It; ++It)
    {
        UTexture2D* Texture = *It;
        UPackage* Package = Texture->GetOutermost();
        if (!Texture->GetPathName().StartsWith(AssetRoot + TEXT("/")) || Package->IsDirty()
            || !FPackageName::DoesPackageExist(Package->GetName())) { continue; }
        UE::Serialization::FEditorBulkData Cached;
        Cached.TearOff();
        if (Registry.TryGetBulkData(Texture->Source.GetPersistentId(), &Cached))
        {
            // UnloadData itself checks whether a disk/virtualized backing is available.
            Cached.UnloadData();
            Registry.UpdateRegistrationData(Package, Cached);
            ++Count;
        }
    }
    return Count;
}

namespace
{
    // Explicit opt-in for isolated memory investigations; normal imports keep this disabled.
    bool IsMeshMemoryTraceEnabled()
    {
        static const bool bEnabled = FPlatformMisc::GetEnvironmentVariable(TEXT("DMA_MESH_MEMORY_TRACE")) == TEXT("1");
        return bEnabled;
    }

    void TraceMeshMemory(const TCHAR* Event, int32 Index, const TCHAR* Name, int64 Vertices = 0, int64 Triangles = 0)
    {
        if (!IsMeshMemoryTraceEnabled()) { return; }
        const FPlatformMemoryStats Stats = FPlatformMemory::GetStats();
        UE_LOG(LogTemp, Display, TEXT("DMA_MESH_MEMORY event=%s index=%d name=%s commit_bytes=%llu physical_bytes=%llu vertices=%lld triangles=%lld"),
            Event, Index, Name, Stats.UsedVirtual, Stats.UsedPhysical, Vertices, Triangles);
        // Preserve the last completed sample even if the external test watchdog stops this process.
        if (GLog) { GLog->Flush(); }
    }

    /** Avoid preloading every mesh payload concurrently in the geometry stage. */
    class FSerialMeshTranslator final : public IDatasmithTranslator
    {
    public:
        explicit FSerialMeshTranslator(TSharedRef<IDatasmithTranslator> InInner, TFunction<void()> InRestoreScene = {})
            : Inner(InInner), RestoreScene(MoveTemp(InRestoreScene)) {}
        virtual FName GetFName() const override { return Inner->GetFName(); }
        virtual void Initialize(FDatasmithTranslatorCapabilities& Capabilities) override
        {
            // FilterElementsToImport has already captured this slice, and CreateSceneAsset has run.
            // Restore full lightweight metadata before the engine computes scene-wide lightmap weights.
            if (RestoreScene) { RestoreScene(); }
            Inner->Initialize(Capabilities);
            Capabilities.bParallelLoadStaticMeshSupported = false;
        }
        virtual bool LoadStaticMesh(const TSharedRef<IDatasmithMeshElement> Element, FDatasmithMeshElementPayload& Payload) override
        {
            ++MeshIndex;
            TraceMeshMemory(TEXT("before_load"), MeshIndex, Element->GetName());
            const bool bLoaded = Inner->LoadStaticMesh(Element, Payload);
            int64 Vertices = 0, Triangles = 0;
            if (IsMeshMemoryTraceEnabled())
            {
                for (const FMeshDescription& Lod : Payload.LodMeshes)
                { Vertices += Lod.Vertices().Num(); Triangles += Lod.Triangles().Num(); }
            }
            TraceMeshMemory(bLoaded ? TEXT("after_load") : TEXT("load_failed"), MeshIndex, Element->GetName(), Vertices, Triangles);
            return bLoaded;
        }
        virtual bool LoadLevelSequence(const TSharedRef<IDatasmithLevelSequenceElement> Element, FDatasmithLevelSequencePayload& Payload) override
        { return Inner->LoadLevelSequence(Element, Payload); }
    private:
        TSharedRef<IDatasmithTranslator> Inner;
        TFunction<void()> RestoreScene;
        int32 MeshIndex = 0;
    };

    TSharedPtr<IDatasmithScene> ReadStoredScene(UDatasmithScene* Asset)
    {
        TSharedPtr<IDatasmithScene> Scene = Asset ? FDatasmithImporterUtils::LoadDatasmithScene(Asset) : nullptr;
        if (Scene) { FDatasmithSceneUtils::CleanUpScene(Scene.ToSharedRef(), false); }
        return Scene;
    }

    UDatasmithScene* LoadBatchScene(const FString& Path)
    {
        if (!Path.StartsWith(TEXT("/Game/")) || !FPackageName::IsValidObjectPath(Path)) { return nullptr; }
        return LoadObject<UDatasmithScene>(nullptr, *Path);
    }
}

TArray<FString> UDatasmithBatchImportLibrary::GetSceneMeshIds(const FString& ScenePath)
{
    check(IsInGameThread());
    TArray<FString> Ids;
    const TSharedPtr<IDatasmithScene> Scene = ReadStoredScene(LoadBatchScene(ScenePath));
    if (Scene)
    {
        for (int32 Index = 0; Index < Scene->GetMeshesCount(); ++Index)
        { Ids.Add(Scene->GetMesh(Index)->GetName()); }
    }
    return Ids;
}

TArray<FString> UDatasmithBatchImportLibrary::ValidateSceneTextures(const FString& ScenePath)
{
    check(IsInGameThread());
    TArray<FString> Issues;
    UDatasmithScene* Asset = LoadBatchScene(ScenePath);
    const TSharedPtr<IDatasmithScene> Scene = ReadStoredScene(Asset);
    if (!Scene) { Issues.Add(TEXT("无法读取已导入场景的源数据。")); return Issues; }
    IAssetRegistry& Registry = FModuleManager::LoadModuleChecked<FAssetRegistryModule>(TEXT("AssetRegistry")).Get();
    for (int32 Index = 0; Index < Scene->GetTexturesCount(); ++Index)
    {
        const TSharedPtr<IDatasmithTextureElement> Element = Scene->GetTexture(Index);
        const TSoftObjectPtr<UTexture>* Texture = Asset->Textures.Find(FName(Element->GetName()));
        if (!Texture || Texture->IsNull() || !Registry.GetAssetByObjectPath(Texture->ToSoftObjectPath()).IsValid())
        { Issues.Add(FString::Printf(TEXT("贴图未完整导入：%s（%s）"), Element->GetName(), Element->GetFile())); }
    }
    return Issues;
}

FDatasmithBatchBindingResult UDatasmithBatchImportLibrary::CheckMaterialBindings(
    const FString& ScenePath, const FString& MeshId, bool bRepair)
{
    check(IsInGameThread());
    FDatasmithBatchBindingResult Result;
    UDatasmithScene* Asset = LoadBatchScene(ScenePath);
    const TSharedPtr<IDatasmithScene> Scene = ReadStoredScene(Asset);
    if (!Scene) { Result.Issues.Add(TEXT("无法读取已导入场景的源数据。")); return Result; }
    TSharedPtr<IDatasmithMeshElement> Element;
    for (int32 Index = 0; Index < Scene->GetMeshesCount(); ++Index)
    {
        if (MeshId == Scene->GetMesh(Index)->GetName()) { Element = Scene->GetMesh(Index); break; }
    }
    const TSoftObjectPtr<UStaticMesh>* MeshReference = Asset->StaticMeshes.Find(FName(*MeshId));
    UStaticMesh* Mesh = MeshReference ? MeshReference->LoadSynchronous() : nullptr;
    if (!Element || !Mesh)
    { Result.Issues.Add(TEXT("静态网格体缺失或源标识不匹配：") + MeshId); return Result; }
    Result.MeshPath = Mesh->GetPathName();
    TMap<FName, FString> Materials;
    for (int32 Index = 0; Index < Element->GetMaterialSlotCount(); ++Index)
    {
        const TSharedPtr<IDatasmithMaterialIDElement> Slot = Element->GetMaterialSlotAt(Index);
        Materials.Add(FName(*FString::FromInt(Slot->GetId())), Slot->GetName());
    }
    UDatasmithStaticMeshTemplate* Template = FDatasmithObjectTemplateUtils::GetObjectTemplate<UDatasmithStaticMeshTemplate>(Mesh);
    TArray<FStaticMaterial> UpdatedSlots = Mesh->GetStaticMaterials();
    TMap<int32, UMaterialInterface*> ExpectedBindings;
    for (int32 Index = 0; Index < Mesh->GetStaticMaterials().Num(); ++Index)
    {
        const FName SlotName = Mesh->GetStaticMaterials()[Index].MaterialSlotName;
        const FString* ExpectedId = Materials.Find(SlotName);
        if (!ExpectedId || ExpectedId->IsEmpty()) { ++Result.UnassignedSlots; continue; }
        ++Result.CheckedSlots;
        const TSoftObjectPtr<UMaterialInterface>* Reference = Asset->Materials.Find(FName(**ExpectedId));
        UMaterialInterface* Expected = Reference ? Reference->LoadSynchronous() : nullptr;
        if (!Expected)
        { Result.Issues.Add(FString::Printf(TEXT("%s 槽 %s：源材质 %s 缺失。"), *Result.MeshPath, *SlotName.ToString(), **ExpectedId)); continue; }
        ExpectedBindings.Add(Index, Expected);
        if (Mesh->GetMaterial(Index) != Expected && bRepair)
        {
            // SetMaterial starts a transaction, loads every source LOD and rebuilds the mesh per slot.
            // These are new batch assets: update the complete slot array once, retaining source slot names.
            UpdatedSlots[Index].MaterialInterface = Expected;
            if (Template)
            {
                for (FDatasmithStaticMaterialTemplate& Slot : Template->StaticMaterials)
                { if (Slot.MaterialSlotName == SlotName) { Slot.MaterialInterface = Expected; } }
            }
            ++Result.RepairedSlots;
        }
    }
    if (Result.RepairedSlots > 0)
    {
        FStaticMeshComponentRecreateRenderStateContext RecreateRenderState(Mesh, false);
        Mesh->SetStaticMaterials(UpdatedSlots);
        Mesh->UpdateUVChannelData(false);
        Mesh->GetOnMeshChanged().Broadcast();
        Mesh->MarkPackageDirty();
    }
    for (const auto& Expected : ExpectedBindings)
    {
        if (Mesh->GetMaterial(Expected.Key) != Expected.Value)
        { Result.Issues.Add(FString::Printf(TEXT("%s 槽 %s：材质绑定校验失败。"), *Result.MeshPath,
            *Mesh->GetStaticMaterials()[Expected.Key].MaterialSlotName.ToString())); }
    }
    if (Mesh->GetStaticMaterials().IsEmpty() && !Materials.IsEmpty())
    { Result.Issues.Add(TEXT("网格体没有保留源材质槽：") + Result.MeshPath); }
    return Result;
}

FDatasmithBatchMemoryStatus UDatasmithBatchImportLibrary::GetMemoryStatus()
{
    const FPlatformMemoryStats Stats = FPlatformMemory::GetStats();
    constexpr double GB = 1024.0 * 1024.0 * 1024.0;
    FDatasmithBatchMemoryStatus Result;
    Result.AvailablePhysicalGB = Stats.AvailablePhysical / GB;
    Result.AvailableVirtualGB = Stats.AvailableVirtual / GB;
    Result.UsedPhysicalGB = Stats.UsedPhysical / GB;
    Result.UsedVirtualGB = Stats.UsedVirtual / GB;
    return Result;
}

void UDatasmithBatchImportLibrary::FinishCompilation()
{
    check(IsInGameThread());
    FAssetCompilingManager::Get().FinishAllCompilation();
}

void UDatasmithBatchImportLibrary::ReleaseTemporaryResources()
{
    check(IsInGameThread());
    // Do not reset the editor undo stack or unload packages referenced by the user's level.
    FAssetCompilingManager::Get().FinishAllCompilation();
    FlushRenderingCommands();
    // Save failures must not make GC discard unsaved imported assets or unrelated user edits.
    TArray<TStrongObjectPtr<UObject>> UnsavedObjects;
    for (TObjectIterator<UObject> It; It; ++It)
    {
        UPackage* Package = It->GetOutermost();
        if (Package && Package->IsDirty() && !Package->HasAnyFlags(RF_Transient)
            && !Package->HasAnyPackageFlags(PKG_CompiledIn))
        { UnsavedObjects.Emplace(*It); }
    }
    CollectGarbage(RF_NoFlags, true);
    FlushRenderingCommands();
    FMemory::Trim();
}

bool UDatasmithBatchImportLibrary::ShowOptions(UDatasmithImportOptions* Options)
{
    if (!Options || !IsInGameThread())
    {
        return false;
    }
    // Match the engine's new-import visibility setup, without persisting global metadata changes.
    FProperty* Reimport = FindFProperty<FProperty>(UDatasmithImportOptions::StaticClass(),
        GET_MEMBER_NAME_CHECKED(UDatasmithImportOptions, ReimportOptions));
    const FString Category = Reimport ? Reimport->GetMetaData(TEXT("Category")) : FString();
    if (Reimport)
    {
        Reimport->SetMetaData(TEXT("Category"), TEXT("NotVisible"));
    }
    ON_SCOPE_EXIT { if (Reimport) { Reimport->SetMetaData(TEXT("Category"), *Category); } };
    FEditorDialogLibraryObjectDetailsViewOptions View;
    View.bShowObjectName = false;
    View.bAllowResizing = true;
    View.MinWidth = 620;
    View.MinHeight = 430;
    return UEditorDialogLibrary::ShowObjectDetailsView(
        FText::FromString(TEXT("Datasmith 批量导入选项（整批共用，仅导入资产）")), Options, View);
}

FDatasmithBatchImportResult UDatasmithBatchImportLibrary::ImportFile(const FString& Filename,
    const FString& DestinationRoot, const FString& SceneName, UDatasmithImportOptions* Options)
{
    return ImportFileStage(Filename, DestinationRoot, SceneName, Options, TEXT("all"));
}

FDatasmithBatchImportResult UDatasmithBatchImportLibrary::ImportFileStage(const FString& Filename,
    const FString& DestinationRoot, const FString& SceneName, UDatasmithImportOptions* Options, const FString& Stage)
{
    return ImportStageInternal(Filename, DestinationRoot, SceneName, Options, Stage, 0, 0);
}

FDatasmithBatchImportResult UDatasmithBatchImportLibrary::ImportGeometryChunk(const FString& Filename,
    const FString& DestinationRoot, const FString& SceneName, UDatasmithImportOptions* Options, int32 StartMeshIndex, int32 MaxMeshes)
{
    if (StartMeshIndex < 0 || MaxMeshes < 1 || MaxMeshes > 24)
    { FDatasmithBatchImportResult Result; Result.Error = TEXT("Invalid geometry slice bounds."); return Result; }
    return ImportStageInternal(Filename, DestinationRoot, SceneName, Options, TEXT("geometry"), StartMeshIndex, MaxMeshes);
}

FDatasmithBatchImportResult UDatasmithBatchImportLibrary::ImportTextureChunk(const FString& Filename,
    const FString& DestinationRoot, const FString& SceneName, UDatasmithImportOptions* Options, int32 StartTextureIndex, int32 MaxTextures)
{
    if (StartTextureIndex < 0 || MaxTextures < 1 || MaxTextures > 8)
    { FDatasmithBatchImportResult Result; Result.Error = TEXT("Invalid texture slice bounds."); return Result; }
    return ImportStageInternal(Filename, DestinationRoot, SceneName, Options, TEXT("textures"), StartTextureIndex, MaxTextures);
}

FDatasmithBatchImportResult UDatasmithBatchImportLibrary::ImportStageInternal(const FString& Filename,
    const FString& DestinationRoot, const FString& SceneName, UDatasmithImportOptions* Options, const FString& Stage,
    int32 StartMeshIndex, int32 MaxMeshes)
{
    FDatasmithBatchImportResult Result;
    const FString FullPath = FPaths::ConvertRelativePathToFull(Filename);
    const FString Target = DestinationRoot / SceneName;
    if (!IsInGameThread() || !Options || (Stage != TEXT("all") && Stage != TEXT("geometry") && Stage != TEXT("materials") && Stage != TEXT("textures") && Stage != TEXT("materials_only"))
        || !IFileManager::Get().FileExists(*FullPath)
        || !FPaths::GetExtension(FullPath).Equals(TEXT("udatasmith"), ESearchCase::IgnoreCase)
        || SceneName.IsEmpty() || SceneName.Contains(TEXT("/")) || SceneName.Contains(TEXT("\\"))
        || !DestinationRoot.StartsWith(TEXT("/Game/"))
        || !FPackageName::IsValidLongPackageName(Target)
        || !FPackageName::IsValidObjectPath(Target / SceneName + TEXT(".") + SceneName))
    {
        Result.Error = TEXT("Invalid source, destination or Datasmith import options.");
        return Result;
    }

    TArray<FAssetData> ExistingAssets;
    const FAssetRegistryModule& Registry = FModuleManager::LoadModuleChecked<FAssetRegistryModule>(TEXT("AssetRegistry"));
    Registry.Get().GetAssetsByPath(FName(*Target), ExistingAssets, true);
    const FString DiskTarget = FPackageName::LongPackageNameToFilename(Target);
    const bool bOnlyMaterials = Stage == TEXT("materials_only");
    const bool bMaterials = Stage == TEXT("materials") || bOnlyMaterials;
    const bool bTextures = Stage == TEXT("textures");
    const bool bChunked = Stage == TEXT("geometry") && MaxMeshes > 0;
    const bool bContinue = bMaterials || bTextures || (bChunked && StartMeshIndex > 0);
    if (!bContinue && (!ExistingAssets.IsEmpty() || FindPackage(nullptr, *Target)
        || IFileManager::Get().DirectoryExists(*DiskTarget)
        || IFileManager::Get().FileExists(*(DiskTarget + TEXT(".uasset")))
        || IFileManager::Get().FileExists(*(DiskTarget + TEXT(".umap")))))
    {
        Result.Error = TEXT("The output name is already occupied. Choose a new suffixed name.");
        return Result;
    }

    using namespace UE::DatasmithImporter;
    const TSharedPtr<FExternalSource> Source = IExternalSourceModule::GetOrCreateExternalSource(FSourceUri::FromFilePath(FullPath));
    if (!Source)
    {
        Result.Error = TEXT("Datasmith could not resolve the source file.");
        return Result;
    }
    // This is the same source-name override used by the engine's Datasmith reimport.
    // Keep the original URI: renaming/copying source XML would damage relative file references.
    const FString PreviousName = Source->GetSceneName();
    TSharedPtr<IDatasmithScene> LoadedScene;
    const TSharedPtr<IDatasmithScene> PreviousScene = Source->GetDatasmithScene();
    const FString PreviousLoadedName = PreviousScene ? FString(PreviousScene->GetName()) : PreviousName;
    Source->SetSceneName(*SceneName);
    ON_SCOPE_EXIT
    {
        Source->SetSceneName(*PreviousName);
        if (LoadedScene)
        {
            LoadedScene->SetName(*PreviousLoadedName);
        }
    };

    // The file URI resolver creates a fresh FExternalSource on each request.
    // Construct the context with THIS source, rather than asking SceneElement for another one.
    FDatasmithImportContext Context(Source, false, FName(TEXT("DatasmithBatchImport")),
        FText::FromString(TEXT("Datasmith Batch Import")));
    Context.bImportedViaScript = true;
    if (bContinue)
    {
        Context.SceneAsset = LoadBatchScene(Target / SceneName + TEXT(".") + SceneName);
        if (!Context.SceneAsset || !Context.SceneAsset->AssetImportData
            || FPaths::ConvertRelativePathToFull(Context.SceneAsset->AssetImportData->GetFirstFilename()) != FullPath)
        { Result.Error = TEXT("第二阶段无法确认第一阶段的场景和源文件，已拒绝覆盖。"); return Result; }
        Context.bIsAReimport = true;
    }
    Context.Options->BaseOptions = Options->BaseOptions;
    Context.Options->ReimportOptions.bUpdateActors = false;
    if (Stage == TEXT("geometry")) { Context.Options->BaseOptions.bIncludeMaterial = false; }
    if (bMaterials || bTextures) { Context.Options->BaseOptions.bIncludeGeometry = false; }
    Context.Options->BaseOptions.SceneHandling = EDatasmithImportScene::AssetsOnly;
    if (!Context.InitOptions(nullptr, TOptional<FString>(), true))
    {
        Result.Error = TEXT("Datasmith import options could not be initialized.");
        return Result;
    }
    // InitOptions derives SceneHandling from reimport flags. Enforce assets-only after that too.
    Context.Options->BaseOptions.SceneHandling = EDatasmithImportScene::AssetsOnly;

    LoadedScene = Source->TryLoad();
    if (!LoadedScene)
    {
        Result.Error = TEXT("Datasmith scene translation failed. See the UE Output Log.");
        return Result;
    }
    // Also handle a source already translated by an earlier import in this process.
    LoadedScene->SetName(*SceneName);
    TArray<TSharedPtr<IDatasmithMeshElement>> CompleteMeshes;
    if (bChunked)
    {
        Result.TotalMeshes = LoadedScene->GetMeshesCount();
        if (StartMeshIndex >= Result.TotalMeshes)
        { Result.Error = TEXT("Geometry slice is outside the source scene."); return Result; }
        Result.NextMeshIndex = FMath::Min(StartMeshIndex + MaxMeshes, Result.TotalMeshes);
        CompleteMeshes.Reserve(Result.TotalMeshes);
        for (int32 Index = 0; Index < Result.TotalMeshes; ++Index) { CompleteMeshes.Add(LoadedScene->GetMesh(Index)); }
        // Only the in-memory translation is sliced. Original XML and external resources stay untouched.
        // The material stage reloads the full source scene before final binding validation.
        for (int32 Index = LoadedScene->GetMeshesCount() - 1; Index >= 0; --Index)
        {
            if (Index < StartMeshIndex || Index >= Result.NextMeshIndex) { LoadedScene->RemoveMeshAt(Index); }
        }
    }
    Context.InitScene(LoadedScene.ToSharedRef());
    if (Stage == TEXT("geometry") && Context.SceneTranslator)
    {
        TFunction<void()> RestoreCompleteScene;
        if (bChunked)
        {
            RestoreCompleteScene = [LoadedScene, CompleteMeshes]()
            {
                LoadedScene->EmptyMeshes();
                for (const TSharedPtr<IDatasmithMeshElement>& Mesh : CompleteMeshes) { LoadedScene->AddMesh(Mesh.ToSharedRef()); }
            };
        }
        Context.SceneTranslator = MakeShared<FSerialMeshTranslator>(Context.SceneTranslator.ToSharedRef(), MoveTemp(RestoreCompleteScene));
    }
    if (!Context.SetupDestination(DestinationRoot, RF_Public | RF_Standalone | RF_Transactional, GWarn, true))
    {
        Result.Error = TEXT("Datasmith output destination could not be initialized.");
        return Result;
    }
    if (bChunked && bContinue)
    {
        // The engine only reserves names in the current source slice. Reserve earlier slices too.
        for (const FAssetData& Asset : ExistingAssets)
        {
            if (Asset.AssetClassPath == UStaticMesh::StaticClass()->GetClassPathName())
            { Context.AssetsContext.StaticMeshNameProvider.AddExistingName(Asset.AssetName.ToString()); }
        }
    }
    bool bCancelled = false;
    TraceMeshMemory(TEXT("before_import"), 0, *Stage);
    bool bImported = false;
    if (bTextures || bOnlyMaterials)
    {
        FDatasmithImporter::FilterElementsToImport(Context);
        if (bTextures)
        {
        Result.TotalTextures = LoadedScene->GetTexturesCount();
        if (MaxMeshes < 1 || StartMeshIndex > Result.TotalTextures || (StartMeshIndex == Result.TotalTextures && StartMeshIndex != 0))
        { Result.Error = TEXT("Texture slice is outside the source scene."); return Result; }
        Result.NextTextureIndex = FMath::Min(StartMeshIndex + MaxMeshes, Result.TotalTextures);
        // Use the engine's import/finalize steps, but defer ALL materials until textures are saved.
        // Full scene metadata and all previously finalized mappings stay in the existing scene asset.
        TSet<FString> SliceIds;
        for (int32 Index = StartMeshIndex; Index < Result.NextTextureIndex; ++Index)
        { SliceIds.Add(LoadedScene->GetTexture(Index)->GetName()); }
        for (int32 Index = Context.FilteredScene->GetTexturesCount() - 1; Index >= 0; --Index)
        {
            if (!SliceIds.Contains(Context.FilteredScene->GetTexture(Index)->GetName()))
            { Context.FilteredScene->RemoveTextureAt(Index); }
        }
        for (const FAssetData& Asset : ExistingAssets)
        { Context.AssetsContext.TextureNameProvider.AddExistingName(Asset.AssetName.ToString()); }
        FDatasmithImporter::ImportTextures(Context);
        }
        else { FDatasmithImporter::ImportMaterials(Context); }
        if (!Context.bUserCancelled)
        {
            FDatasmithImporter::FinalizeImport(Context, TSet<UObject*>());
            Context.SceneAsset->MarkPackageDirty();
            bImported = true;
        }
        bCancelled = Context.bUserCancelled;
    }
    else { bImported = DatasmithImportFactoryImpl::ImportDatasmithScene(Context, bCancelled); }
    TraceMeshMemory(TEXT("after_import"), 0, *Stage);
    Result.Scene = Context.SceneAsset;
    Result.bSuccess = bImported && !bCancelled && IsValid(Result.Scene);
    Result.ActorCount = Context.GetImportedActors().Num();
    if (bChunked && Result.bSuccess)
    {
        // Even if no mesh in this slice was referenced, Initialize may not have been called.
        LoadedScene->EmptyMeshes();
        for (const TSharedPtr<IDatasmithMeshElement>& Mesh : CompleteMeshes) { LoadedScene->AddMesh(Mesh.ToSharedRef()); }
        FDatasmithImporterUtils::SaveDatasmithScene(LoadedScene.ToSharedRef(), Context.SceneAsset);
        Context.SceneAsset->MarkPackageDirty();
    }
    if (bMaterials && Result.bSuccess)
    {
        Context.SceneAsset->AssetImportData->BaseOptions = Options->BaseOptions;
        Context.SceneAsset->AssetImportData->BaseOptions.SceneHandling = EDatasmithImportScene::AssetsOnly;
        Context.SceneAsset->MarkPackageDirty();
    }
    if (Source->GetAssetTranslator()) { Source->GetAssetTranslator()->UnloadScene(); }
    Context.DisplayMessages();
    if (!Result.bSuccess)
    {
        Result.Error = TEXT("Datasmith import failed or was cancelled. See the UE Output Log.");
    }
    return Result;
}

FDatasmithBatchImportResult UDatasmithBatchImportLibrary::ImportMaterialsOnly(const FString& Filename,
    const FString& DestinationRoot, const FString& SceneName, UDatasmithImportOptions* Options)
{
    return ImportStageInternal(Filename, DestinationRoot, SceneName, Options, TEXT("materials_only"), 0, 0);
}
