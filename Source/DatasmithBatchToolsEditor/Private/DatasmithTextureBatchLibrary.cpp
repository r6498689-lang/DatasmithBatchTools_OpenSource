#include "DatasmithTextureBatchLibrary.h"
#include "Engine/Texture2D.h"
#include "Engine/TextureLODSettings.h"
#include "DeviceProfiles/DeviceProfile.h"
#include "DeviceProfiles/DeviceProfileManager.h"
#include "Materials/MaterialInterface.h"
#include "TextureCompiler.h"

FIntPoint UDatasmithTextureBatchLibrary::GetTextureSourceSize(UTexture2D* Texture)
{
    if (!IsValid(Texture) || !Texture->Source.IsValid()) return FIntPoint::ZeroValue;
    return FIntPoint(Texture->Source.GetSizeX(), Texture->Source.GetSizeY());
}

FString UDatasmithTextureBatchLibrary::GetTextureSourceIdentity(UTexture2D* Texture)
{
    return IsValid(Texture) && Texture->Source.IsValid() ? Texture->Source.GetId().ToString() : FString();
}

bool UDatasmithTextureBatchLibrary::TextureHasEffectiveMips(UTexture2D* Texture)
{
    if (!IsValid(Texture)) return false;
    if (Texture->VirtualTextureStreaming) return true;
    TextureMipGenSettings MipSettings = Texture->MipGenSettings;
    if (const UDeviceProfile* Profile = UDeviceProfileManager::Get().GetActiveProfile())
    {
        if (const UTextureLODSettings* Settings = Profile->GetTextureLODSettings())
        {
            float Sharpen = 0.0f;
            uint32 KernelSize = 0;
            bool Average = false, NoColorShift = false, BorderBlack = false;
            Settings->GetMipGenSettings(*Texture, MipSettings, Sharpen, KernelSize, Average, NoColorShift, BorderBlack);
        }
    }
    return MipSettings != TMGS_NoMipmaps;
}

TArray<UTexture2D*> UDatasmithTextureBatchLibrary::GetMaterialTextureAssets(UMaterialInterface* Material)
{
    TArray<UTexture2D*> Result;
    if (IsValid(Material))
    {
        TArray<UTexture*> Textures;
        Material->GetUsedTextures(Textures, EMaterialQualityLevel::Num, true, ERHIFeatureLevel::Num, true);
        for (UTexture* Texture : Textures)
        {
            if (UTexture2D* Texture2D = Cast<UTexture2D>(Texture)) Result.AddUnique(Texture2D);
        }
    }
    return Result;
}

FString UDatasmithTextureBatchLibrary::ApplyTextureProperties(UTexture2D* Texture, const TMap<FName, double>& Properties)
{
    if (!IsValid(Texture) || !Texture->Source.IsValid()) return TEXT("Missing Texture2D source art");
    if (Properties.IsEmpty()) return TEXT("No properties specified");
    for (const TPair<FName, double>& Pair : Properties)
    {
        const FString Key = Pair.Key.ToString();
        const double Value = Pair.Value;
        if (!FMath::IsFinite(Value)) return TEXT("Non-finite property value");
        if (Key == TEXT("max_texture_size"))
        {
            if (Value < 0 || Value > 65536 || Value != FMath::FloorToDouble(Value)) return TEXT("Invalid maximum size");
        }
        else if (Key == TEXT("lod_bias"))
        {
            if (Value < -32 || Value > 32 || Value != FMath::FloorToDouble(Value)) return TEXT("Invalid LOD bias");
        }
        else if (Key == TEXT("adjust_brightness") || Key == TEXT("adjust_saturation"))
        {
            if (Value < 0 || Value > MAX_flt) return TEXT("Invalid color multiplier");
        }
        else if (Key == TEXT("adjust_vibrance"))
        {
            if (Value < 0 || Value > 1) return TEXT("Invalid vibrance");
        }
        else if (Key != TEXT("adjust_hue")) return FString::Printf(TEXT("Unsupported property: %s"), *Key);
    }
    // Finish earlier compilation before touching properties, then rebuild only once.
    UTexture* Textures[] = {Texture};
    FTextureCompilingManager::Get().FinishCompilation(MakeArrayView(Textures));
    Texture->Modify();
    for (const TPair<FName, double>& Pair : Properties)
    {
        const FString Key = Pair.Key.ToString();
        if (Key == TEXT("max_texture_size")) Texture->MaxTextureSize = static_cast<int32>(Pair.Value);
        else if (Key == TEXT("lod_bias")) Texture->LODBias = static_cast<int32>(Pair.Value);
        else if (Key == TEXT("adjust_brightness")) Texture->AdjustBrightness = static_cast<float>(Pair.Value);
        else if (Key == TEXT("adjust_saturation")) Texture->AdjustSaturation = static_cast<float>(Pair.Value);
        else if (Key == TEXT("adjust_hue")) Texture->AdjustHue = static_cast<float>(Pair.Value);
        else if (Key == TEXT("adjust_vibrance")) Texture->AdjustVibrance = static_cast<float>(Pair.Value);
    }
    Texture->PostEditChange();
    FTextureCompilingManager::Get().FinishCompilation(MakeArrayView(Textures));
    return FString();
}
