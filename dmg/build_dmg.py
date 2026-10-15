from pathlib import Path
import dmgbuild

root = Path(__file__).resolve().parent.parent
dmgbuild.build_dmg(
    str(root / "OTFeatFreezer.dmg"),
    "OTFeatFreezer",
    settings_file=str(root / "dmg" / "dmg_settings.py"),
    defines={"app": str(root / "dist" / "OTFeatFreezer.app")},
)
