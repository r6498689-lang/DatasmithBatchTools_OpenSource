#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "DatasmithTextureBatchLibrary.generated.h"

class UTexture2D;
class UMaterialInterface;

/** Editor-only source validation and a single-rebuild texture property writer. */
UCLASS()
class DATASMITHBATCHTOOLSEDITOR_API UDatasmithTextureBatchLibrary final : public UBlueprintFunctionLibrary
{
    GENERATED_BODY()
public:
    /** Zero dimensions mean unavailable source art; never guesses from runtime mip size. */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Texture Batch")
    static FIntPoint GetTextureSourceSize(UTexture2D* Texture);

    UFUNCTION(BlueprintCallable, Category = "Datasmith Texture Batch")
    static FString GetTextureSourceIdentity(UTexture2D* Texture);

    UFUNCTION(BlueprintCallable, Category = "Datasmith Texture Batch")
    static bool TextureHasEffectiveMips(UTexture2D* Texture);

    /** Includes instance overrides, material functions and all quality/feature permutations. */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Texture Batch")
    static TArray<UTexture2D*> GetMaterialTextureAssets(UMaterialInterface* Material);

    /** Validates the whole map before modifying. Empty string means success. Does not save. */
    UFUNCTION(BlueprintCallable, Category = "Datasmith Texture Batch")
    static FString ApplyTextureProperties(UTexture2D* Texture, const TMap<FName, double>& Properties);
};
