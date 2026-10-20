#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "DatasmithBatchImportLibrary.generated.h"

class UDatasmithScene;
class UDatasmithImportOptions;

USTRUCT(BlueprintType)
struct FDatasmithBatchMemoryStatus
{
    GENERATED_BODY()
    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    double AvailablePhysicalGB = 0;
    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    double AvailableVirtualGB = 0;
    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    double UsedPhysicalGB = 0;
    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    double UsedVirtualGB = 0;
};

USTRUCT(BlueprintType)
struct FDatasmithBatchImportResult
{
    GENERATED_BODY()

    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    bool bSuccess = false;

    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    TObjectPtr<UDatasmithScene> Scene = nullptr;

    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    int32 ActorCount = 0;

    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    FString Error;

    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    int32 NextMeshIndex = 0;
    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    int32 TotalMeshes = 0;
    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    int32 NextTextureIndex = 0;
    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    int32 TotalTextures = 0;
};

USTRUCT(BlueprintType)
struct FDatasmithBatchBindingResult
{
    GENERATED_BODY()
    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    FString MeshPath;
    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    int32 CheckedSlots = 0;
    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    int32 RepairedSlots = 0;
    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    int32 UnassignedSlots = 0;
    UPROPERTY(BlueprintReadOnly, Category = "Datasmith Batch Import")
    TArray<FString> Issues;
};

/** One synchronous import. The Python queue owns naming, saving and cancellation. */
UCLASS()
class DATASMITHBATCHTOOLSEDITOR_API UDatasmithBatchImportLibrary final : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()
public:
    /** Drop reloadable registry payload copies only for saved textures inside this output folder. */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Batch Import")
    static int32 ReleaseSavedSourceCache(const FString& AssetRoot);

    /** Stage is all, geometry, or materials. Materials continues the scene created by geometry. */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Batch Import")
    static FDatasmithBatchImportResult ImportFileStage(const FString& Filename, const FString& DestinationRoot,
        const FString& SceneName, UDatasmithImportOptions* Options, const FString& Stage);

    /** Import a bounded geometry slice. Callers must save and collect before the next slice. */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Batch Import")
    static FDatasmithBatchImportResult ImportGeometryChunk(const FString& Filename, const FString& DestinationRoot,
        const FString& SceneName, UDatasmithImportOptions* Options, int32 StartMeshIndex, int32 MaxMeshes = 24);

    UFUNCTION(BlueprintCallable, Category = "Datasmith Batch Import")
    static FDatasmithBatchImportResult ImportTextureChunk(const FString& Filename, const FString& DestinationRoot,
        const FString& SceneName, UDatasmithImportOptions* Options, int32 StartTextureIndex, int32 MaxTextures = 8);

    /** Continue a scene whose textures have already been imported; never decode source images again. */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Batch Import")
    static FDatasmithBatchImportResult ImportMaterialsOnly(const FString& Filename, const FString& DestinationRoot,
        const FString& SceneName, UDatasmithImportOptions* Options);

    UFUNCTION(BlueprintCallable, Category = "Datasmith Batch Import")
    static TArray<FString> GetSceneMeshIds(const FString& ScenePath);

    UFUNCTION(BlueprintCallable, Category = "Datasmith Batch Import")
    static TArray<FString> ValidateSceneTextures(const FString& ScenePath);

    /** Validates one mesh at a time so callers can save and collect between meshes. */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Batch Import")
    static FDatasmithBatchBindingResult CheckMaterialBindings(const FString& ScenePath, const FString& MeshId,
        bool bRepair = true);

    UFUNCTION(BlueprintCallable, Category = "Datasmith Batch Import")
    static FDatasmithBatchMemoryStatus GetMemoryStatus();

    UFUNCTION(BlueprintCallable, Category = "Datasmith Batch Import")
    static void FinishCompilation();

    /** Called only after compilation and Save All; does not unload or delete user assets. */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Batch Import")
    static void ReleaseTemporaryResources();

    /** One native options dialog for a new-import batch; hides inapplicable reimport controls. */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Batch Import")
    static bool ShowOptions(UDatasmithImportOptions* Options);

    /** Imports into DestinationRoot/SceneName, preserving the original source URI. Never saves. */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Batch Import")
    static FDatasmithBatchImportResult ImportFile(const FString& Filename, const FString& DestinationRoot,
        const FString& SceneName, UDatasmithImportOptions* Options);
private:
    static FDatasmithBatchImportResult ImportStageInternal(const FString& Filename, const FString& DestinationRoot,
        const FString& SceneName, UDatasmithImportOptions* Options, const FString& Stage, int32 StartMeshIndex, int32 MaxMeshes);
};
