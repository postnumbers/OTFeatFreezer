# dmg_settings.py
import os

app = defines.get("app", "dist/OTFeatFreezer.app")
appname = os.path.basename(app)

format = "UDZO"
files = [app]
symlinks = {"Applications": "/Applications"}

window_rect = ((200, 120), (600, 400))
icon_size = 100
icon_locations = {
    appname: (150, 190),
    "Applications": (450, 190),
}