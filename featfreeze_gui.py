#!/usr/bin/env python3
"""
featfreeze_gui.py -- a PySide6 front end for freezing OpenType features into fonts.

The processing is done by the "opentype-feature-freezer" library (the engine
behind the `pyftfeatfreeze` command and the OTFeatureFreezer app), so every
control maps to one of its options and carries the same description.

Install the requirements, then run the script:

    pip install PySide6 fonttools opentype-feature-freezer
    python featfreeze_gui.py
"""

from __future__ import annotations

import contextlib
import html
import io
import logging
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path
from types import SimpleNamespace

try:
    from fontTools import agl
    from fontTools import unicodedata as ft_unicodedata
    from fontTools.pens.qtPen import QtPen
    from fontTools.ttLib import TTFont
    from opentype_feature_freezer import RemapByOTL
    from PySide6.QtCore import QByteArray, QEvent, QSettings, Qt, QThread, QTimer, Signal
    from PySide6.QtCore import QPoint, QPointF, QRectF, QSize
    from PySide6.QtGui import (
        QPainter, QPainterPath, QPixmap, QTransform,
        QColor, QFont, QFontDatabase, QIcon, QKeySequence, QPalette, QTextCursor,
        QTextDocument)
    from PySide6.QtWidgets import (
        QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
        QDialogButtonBox, QFileDialog, QFrame,
        QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
        QListWidgetItem, QMainWindow, QMenu, QSizePolicy, QTreeWidget, QTreeWidgetItem,
        QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
        QSpinBox, QSplitter, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
        QTextEdit, QToolButton, QVBoxLayout, QWidget,
    )
except ImportError as exc:  # pragma: no cover
    sys.exit(
        f"Missing dependency: {exc.name}\n"
        "Install the requirements with:\n"
        "    pip install PySide6 fonttools opentype-feature-freezer"
    )

FONT_FILTER = "Fonts (*.otf *.ttf *.woff *.woff2);;All files (*)"
FONT_EXTENSIONS = (".otf", ".ttf", ".woff", ".woff2")  # accepted by drag and drop
PREVIEW_CHAR_LIMIT = 500
FEATURE_COLUMNS = 1  # raise this for a wider, multi-column feature list
# Sequences shown in the preview because fonts often turn them into ligatures.
STANDARD_LIGATURES = ("ff", "fi", "fl", "ffi", "ffl", "fj", "ft", "tt", "Th")
CODING_LIGATURES = (
    "->", "=>", "<-", "<->", "<=>", "==", "!=", "===", "!==", "<=", ">=",
    "::", "++", "--", "&&", "||", "//", "/*", "*/", "...", "|>", "<|",
    ">>", "<<", "??", "?.", ";;", "#{", "www",
)

FREEZER_LOGGER = logging.getLogger("opentype_feature_freezer")
FREEZER_LOGGER.addHandler(logging.NullHandler())
FREEZER_LOGGER.propagate = False
FREEZER_LOGGER.setLevel(logging.ERROR)

# Descriptions taken from the OTFeatureFreezer app / `pyftfeatfreeze --help`.
APP_DESCRIPTION = (
    'With pyftfeatfreeze you can "freeze" some OpenType features into a font. '
    'These features are then "on by default", even in apps that don\'t support '
    'OpenType features. Internally, the tool remaps the "cmap" table of the '
    "font by applying the specified GSUB features. Only single and alternate "
    "substitutions are supported."
)
GROUP_FREEZING = "options to control feature freezing"
GROUP_RENAMING = "options to control font renaming"
GROUP_REPORTING = "reporting options"
HELP = {
    "inpath": "input .otf or .ttf font file",
    "outpath": "output .otf or .ttf font file (optional)",
    "features": "comma-separated list of OpenType feature tags, e.g. 'smcp,c2sc,onum'",
    "script": "OpenType script tag, e.g. 'cyrl' (optional)",
    "lang": "OpenType language tag, e.g. 'SRB ' (optional)",
    "zapnames": "zap glyphnames from the font ('post' table version 3, .ttf only)",
    "suffix": "add a suffix to the font family name (by default, the suffix "
              "will be constructed from the OpenType feature tags)",
    "usesuffix": "use a custom suffix when --suffix is enabled",
    "replacenames": "search for strings in the font naming tables and replace "
                    "or remove them, format is 'search1/replace1,word,...'",
    "info": "update font version string",
    "report": "report languages, scripts and features in font",
    "names": "output names of remapped glyphs during processing",
    "verbose": "print additional information during processing",
}
# Labels shown on the controls (the app's option names are kept in HELP keys).
LABELS = {
    "zapnames": "zap glyph names",
    "usesuffix": "custom suffix",
    "replacenames": "replace names",
    "info": "update font version string",
    "report": "report languages, scripts and features",
    "names": "names",
    "verbose": "verbose",
}
# Text of the (?) help popups, based on the OTFeatureFreezer descriptions.
POPUP = {
    "script": "OpenType script tag, e.g. 'cyrl' (optional). Leave empty to "
              "use all scripts.",
    "lang": "OpenType language tag, e.g. 'SRB ' (optional). Leave empty to "
            "use the default language.",
    "zapnames": "Zap glyph names from the font ('post' table version 3, .ttf "
                "only). Makes the file smaller.",
    "usesuffix": "Text added to the font family name, e.g. 'SC' turns "
                 "'Inter' into 'Inter SC'. Leave empty to keep the family "
                 "name. The full name, PostScript name and output file name "
                 "follow the new family name. OpenType feature tags are never "
                 "added to the family name.",
    "replacenames": "Rename the font family and its styles by replacing "
                    "text in their names, e.g. Inter/Outer renames the Inter "
                    "family to Outer, and Bold/Heavy renames the Bold style to "
                    "Heavy. To remove words, enter them without a slash: "
                    "OT 2 turns Hermes Maia OT 2 into Hermes Maia, and Thin "
                    "turns the Thin Italic style into Italic. Words are removed "
                    "whole, so Thin leaves Thinner alone. "
                    "Each pair is applied to both the family name and "
                    "the style name; the search is case-sensitive. The full "
                    "name, PostScript name and output file name follow the new "
                    "names. The basic style name used for style linking "
                    "(Regular, Italic, Bold, Bold Italic) is kept; the new "
                    "style name is stored as the typographic style name. "
                    "Format: search1/replace1,search2/replace2,word,...",
    "info": "Update the font version string by appending '; featfreeze: "
            "<features>'. Only takes effect together with a suffix or "
            "replace names.",
    "report": "Report the languages, scripts and features of each font in "
              "the processing output. The fonts are still saved.",
    "names": "Output the names of remapped glyphs during processing: the "
             "replacement glyph of each remapped character.",
    "verbose": "Print additional information during processing: each lookup, "
               "remap, new name and saved path.",
    "inpath": "Input .otf or .ttf font files and the file name each will be "
              "saved as. Add fonts with 'Add fonts…', or drag files or folders "
              "in from Finder. Fonts may have different features: a ticked "
              "feature is frozen in the fonts that have it, and fonts without "
              "it are still processed. "
              "Select a font to show its features, preview and PostScript name.",
    "output": "Folder for the output .otf or .ttf font files. Each file is "
              "named after the font's new name; input fonts are never "
              "overwritten.",
    "features": "Tick the features to freeze. The button beside each feature "
                "shows how many glyph substitutions it can freeze; click it to "
                "see them and switch individual substitutions on or off for "
                "the output fonts. Features marked 'not in all fonts' are "
                "frozen only in the fonts that have them; 'not in this font' "
                "features come from other input fonts (select one of those "
                "fonts to preview them).",
}

FEATURE_NAMES = {
    "aalt": "Access All Alternates", "c2pc": "Petite Caps from Capitals",
    "c2sc": "Small Caps from Capitals", "calt": "Contextual Alternates",
    "case": "Case-Sensitive Forms", "ccmp": "Glyph Composition",
    "clig": "Contextual Ligatures", "cpsp": "Capital Spacing",
    "cswh": "Contextual Swash", "dlig": "Discretionary Ligatures",
    "dnom": "Denominators", "frac": "Fractions", "hist": "Historical Forms",
    "hlig": "Historical Ligatures", "liga": "Standard Ligatures",
    "lnum": "Lining Figures", "locl": "Localized Forms",
    "nalt": "Alternate Annotation Forms", "numr": "Numerators",
    "onum": "Oldstyle Figures", "ordn": "Ordinals", "ornm": "Ornaments",
    "pcap": "Petite Capitals", "pnum": "Proportional Figures",
    "rlig": "Required Ligatures", "salt": "Stylistic Alternates",
    "sinf": "Scientific Inferiors", "smcp": "Small Capitals",
    "subs": "Subscript", "sups": "Superscript", "swsh": "Swash",
    "titl": "Titling", "tnum": "Tabular Figures", "unic": "Unicase",
    "zero": "Slashed Zero",
}

# --------------------------------------------------------------------------- #
# Appearance
# --------------------------------------------------------------------------- #

THEMES = {
    # bg: the window; field: text fields and checkboxes; neutral: main button
    "light": dict(bg="#ecece8", field="#ffffff", neutral="#d6d6d0", fg="#1d1d1f", muted="#66666b", border="#c2c2bc",
                  accent="#6b6b70", on_accent="#ffffff", changed="#2f6bc4", pressed="#e3e3de"),
    "dark": dict(bg="#1f2023", field="#303137", neutral="#45464d", fg="#e8e8ea", muted="#9d9da3", border="#505157",
                 accent="#a3a3a9", on_accent="#1f2023", changed="#85b4f2", pressed="#2d2e33"),
}

STYLE_SHEET = """
QLabel[role="appTitle"] { font-size: 22px; font-weight: 600; }
QLabel[role="title"]    { font-size: 18px; font-weight: 600; }
QLabel[role="subtitle"] { font-weight: 600; }
QLabel[role="hint"]     { color: %(muted)s; }
QLineEdit, QTextEdit, QPlainTextEdit, QListWidget, QTreeWidget {
    background-color: %(field)s; color: %(fg)s;
    border: 1px solid %(border)s; border-radius: 5px;
}
QScrollArea {
    background-color: %(bg)s; color: %(fg)s;
    border: 1px solid %(border)s; border-radius: 5px;
}
QLineEdit { padding: 3px 6px; }
QComboBox, QSpinBox {
    background-color: %(field)s; color: %(fg)s;
    border: 1px solid %(border)s; border-radius: 5px; padding: 3px 6px;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow { image: url(%(arrow_down)s); width: 12px; height: 12px; }
QComboBox QAbstractItemView {
    background-color: %(field)s; color: %(fg)s; border: 1px solid %(border)s;
    selection-background-color: %(accent)s; selection-color: %(on_accent)s;
}
QSpinBox::up-button, QSpinBox::down-button { border: none; width: 18px; }
QSpinBox::up-arrow   { image: url(%(arrow_up)s); width: 10px; height: 10px; }
QSpinBox::down-arrow { image: url(%(arrow_down)s); width: 10px; height: 10px; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator {
    width: 14px; height: 14px; background-color: %(field)s;
    border: 1px solid %(border)s; border-radius: 3px;
}
QCheckBox::indicator:hover { border-color: %(accent)s; }
QCheckBox::indicator:checked {
    background-color: %(accent)s; border-color: %(accent)s; image: url(%(check)s);
}
QListView::indicator {
    width: 14px; height: 14px; background-color: %(field)s;
    border: 1px solid %(border)s; border-radius: 3px;
}
QListView::indicator:checked {
    background-color: %(accent)s; border-color: %(accent)s; image: url(%(check)s);
}
QListView::indicator:indeterminate { image: url(%(dash)s); }
QListView::indicator:disabled { background-color: %(bg)s; }
QListView::item:disabled { color: %(muted)s; }
QPushButton#small { padding: 1px 8px; color: %(muted)s; }
QListWidget, QTreeWidget, QCheckBox#feature { font-size: %(list_pt)spt; }
QHeaderView::section {
    background-color: %(field)s; color: %(muted)s; border: none;
    border-bottom: 1px solid %(border)s; padding: 4px 6px;
}
QPushButton#small[partial="true"] { color: %(fg)s; font-weight: 600; }
QLineEdit:disabled { color: %(muted)s; }
QScrollArea#plain { border: none; }
QTreeWidget[dropTarget="true"] { border: 2px solid %(accent)s; }
QPushButton {
    background-color: %(bg)s; color: %(fg)s;
    border: 1px solid %(border)s; border-radius: 5px; padding: 4px 12px;
}
QPushButton:hover    { border-color: %(accent)s; }
QPushButton:pressed  { background-color: %(pressed)s; }
QPushButton:disabled { color: %(muted)s; }
QPushButton#primary {
    background-color: %(neutral)s; font-weight: 600; padding: 6px 16px;
}
QPushButton#primary:pressed  { background-color: %(border)s; }
QPushButton#primary:disabled { background-color: %(bg)s; }
QPushButton#theme { padding: 0; }
QSplitter::handle { background-color: %(bg)s; }
QToolButton#help { border: none; background: transparent; padding: 0; }
QFrame#helpPopup {
    background-color: %(field)s; border: 1px solid %(border)s; border-radius: 6px;
}
QToolTip { background-color: %(bg)s; color: %(fg)s; border: 1px solid %(border)s; }
"""


_ICON_FOLDER = ""
_ICON_SHAPES = {  # name: (outline, stroke width); colours are set in theme_icons
    "help": ("M8 1.6a6.4 6.4 0 1 0 0 12.8a6.4 6.4 0 1 0 0-12.8"
             "M6.15 6.25a1.9 1.9 0 1 1 2.65 1.75c-.5.22-.8.62-.8 1.15v.35"
             "M8 11.35v.15", 1.3),
    "check": ("M3.5 8.5l3 3 6-7", 2),
    "dash": ("M4.5 8h7", 2),
    "arrow_down": ("M4 6l4 4 4-4", 2),
    "arrow_up": ("M4 10l4-4 4 4", 2),
    "sun": ("M8 5.2a2.8 2.8 0 1 0 0 5.6a2.8 2.8 0 1 0 0-5.6M8 1v1.6M8 13.4V15"
            "M1 8h1.6M13.4 8H15M3.05 3.05l1.13 1.13M11.82 11.82l1.13 1.13"
            "M3.05 12.95l1.13-1.13M11.82 4.18l1.13-1.13", 1.4),
    "moon": ("M13.2 9.6A5.6 5.6 0 1 1 6.4 2.8a4.4 4.4 0 0 0 6.8 6.8z", 1.4),
}


def theme_icons(name: str) -> dict:
    """Write the small check and arrow icons for a theme; returns their paths."""
    global _ICON_FOLDER
    if not _ICON_FOLDER:
        _ICON_FOLDER = tempfile.mkdtemp(prefix="featfreeze_gui_")
    paths = {}
    for shape, (outline, width) in _ICON_SHAPES.items():
        color = THEMES[name][{"check": "on_accent", "help": "muted",
                              "dash": "muted"}.get(shape, "fg")]
        path = os.path.join(_ICON_FOLDER, f"{shape}_{name}.svg")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                f'<path d="{outline}" fill="none" stroke="{color}" '
                f'stroke-width="{width}" stroke-linecap="round" '
                'stroke-linejoin="round"/></svg>')
        paths[shape] = path.replace(os.sep, "/")
    return paths


def apply_theme(app: QApplication, name: str) -> None:
    """Give the whole UI one background colour, in a light or dark variant."""
    colors = dict(THEMES[name], **theme_icons(name))
    # File names, features and substitutions: a point larger than the default
    # (macOS otherwise draws lists a size smaller than the rest of the text).
    base = QApplication.font().pointSizeF()
    colors["list_pt"] = f"{(base if base > 0 else 13) + 1:g}"
    bg, fg = QColor(colors["bg"]), QColor(colors["fg"])
    muted, border = QColor(colors["muted"]), QColor(colors["border"])
    role = QPalette.ColorRole
    palette = QPalette()
    for item in (role.Window, role.AlternateBase, role.Button,
                 role.ToolTipBase, role.Light, role.Midlight):
        palette.setColor(item, bg)
    palette.setColor(role.Base, QColor(colors["field"]))
    for item in (role.WindowText, role.Text, role.ButtonText, role.ToolTipText):
        palette.setColor(item, fg)
        palette.setColor(QPalette.ColorGroup.Disabled, item, muted)
    for item in (role.Mid, role.Dark, role.Shadow):
        palette.setColor(item, border)
    palette.setColor(role.PlaceholderText, muted)
    palette.setColor(role.Highlight, QColor(colors["accent"]))
    palette.setColor(role.HighlightedText, QColor(colors["on_accent"]))
    app.setPalette(palette)
    app.setStyleSheet(STYLE_SHEET % colors)


def system_theme(app: QApplication) -> str:
    try:
        if app.styleHints().colorScheme() == Qt.ColorScheme.Dark:
            return "dark"
    except AttributeError:  # Qt older than 6.5
        pass
    return "light"


# --------------------------------------------------------------------------- #
# Font inspection and processing (no GUI code in this section)
# --------------------------------------------------------------------------- #

def pad_tag(tag: str) -> str:
    """OpenType tags are four characters, padded with spaces ('ROM' -> 'ROM ')."""
    tag = tag.strip()
    return tag.ljust(4) if tag else ""


def empty_font_info() -> dict:
    return {"features": [], "scripts": {}, "cmap": {}, "frozen": {},
            "zapped": set(), "ligatures": {"standard": [], "coding": []}}


def zapped_glyphs(font: TTFont) -> set[str]:
    """Glyph names made up by fontTools because the font's names were zapped
    ('post' table version 3 in a TrueType font): 'glyph00123' and the like."""
    if "post" not in font or font["post"].formatType != 3.0:
        return set()
    if "CFF " in font or "CFF2" in font:  # CFF fonts keep names of their own
        return set()
    return {name for name in font.getGlyphOrder()
            if re.fullmatch(r"glyph\d+", name)}


def undeterminable(source: str, target: str, encoded, zapped) -> bool:
    """True when zapped glyph names hide whether a substitution was frozen:
    the source glyph has no character and no real name, the target has one."""
    return source in zapped and source not in encoded and target in encoded


def in_effect(source: str, target: str, encoded) -> bool:
    """Whether a substitution already shows in the font as it is: frozen, or
    from a glyph without a character (freezing it changes nothing)."""
    return source not in encoded or is_frozen(source, target, encoded)


def pair_wanted(tag: str, glyph: str, effect: bool, ticked, choices) -> bool:
    """Whether a substitution of feature `tag` should be frozen in the output.

    `choices` holds the user's choices: "off" and "on" map feature tags to
    source glyphs unticked or ticked in the substitution list, and features in
    "keep" leave glyphs nobody touched as they are in each font (`effect`).
    """
    if tag not in ticked or glyph in choices.get("off", {}).get(tag, ()):
        return False
    if tag in choices.get("keep", ()) and not effect:
        return glyph in choices.get("on", {}).get(tag, ())
    return True


def frozen_features(path: str) -> set[str]:
    """Tags of the features already frozen (fully or partly) into a font."""
    font = TTFont(path, lazy=True)
    try:
        if "GSUB" not in font:
            return set()
        encoded = encoded_glyphs(font.getBestCmap() or {})
        gsub = font["GSUB"].table
        lookups = gsub.LookupList.Lookup if gsub.LookupList else []
        found = set()
        for record in gsub.FeatureList.FeatureRecord if gsub.FeatureList else []:
            if record.FeatureTag in found:
                continue
            for index in record.Feature.LookupListIndex:
                if any(is_frozen(a, b, encoded)
                       for a, b in lookup_pairs(lookups[index])):
                    found.add(record.FeatureTag)
                    break
        return found
    finally:
        font.close()


def lookup_pairs(lookup) -> list[tuple[str, str]]:
    """The single and alternate substitutions of a lookup (an alternate
    substitution gives its first alternate, as the freezer does)."""
    pairs = []
    for subtable in lookup.SubTable:
        if subtable.LookupType == 7:
            subtable = subtable.ExtSubTable
        if subtable.LookupType == 1:
            pairs.extend(subtable.mapping.items())
        elif subtable.LookupType == 3:
            pairs.extend((glyph, alternates[0]) for glyph, alternates
                         in subtable.alternates.items() if alternates)
    return pairs


def find_ligatures(font: TTFont, cmap: dict, limit: int = 40) -> dict:
    """Character sequences worth previewing because they may form ligatures.

    "standard" holds letter sequences (fi, ffl ...), "coding" holds symbol
    sequences (->, != ...). Sequences found in the font's ligature lookups
    come first, followed by the usual candidates the font has characters for.
    """
    glyph_to_char: dict[str, str] = {}
    for codepoint in sorted(cmap, reverse=True):  # lowest code point wins
        glyph_to_char[cmap[codepoint]] = chr(codepoint)
    found: set[str] = set()
    feature_tags: set[str] = set()
    if "GSUB" in font:
        gsub = font["GSUB"].table
        if gsub.FeatureList:
            feature_tags = {r.FeatureTag for r in gsub.FeatureList.FeatureRecord}
        for lookup in (gsub.LookupList.Lookup if gsub.LookupList else []):
            for subtable in lookup.SubTable:
                if subtable.LookupType == 7:
                    subtable = subtable.ExtSubTable
                if subtable.LookupType != 4:
                    continue
                for first, ligatures in subtable.ligatures.items():
                    for ligature in ligatures:
                        glyphs = [first, *ligature.Component]
                        if all(glyph in glyph_to_char for glyph in glyphs):
                            found.add("".join(glyph_to_char[g] for g in glyphs))

    def usable(text: str) -> bool:
        return all(unicodedata.category(c)[0] not in "CZM" for c in text)

    def available(candidates) -> list[str]:
        return [c for c in candidates if all(ord(ch) in cmap for ch in c)]

    # Keep plain-letter sequences (fi, ffl) and plain-symbol sequences (->, !=);
    # ligatures of accented or non-Latin characters would crowd the preview.
    found = {text for text in found if text.isascii() and usable(text)}
    standard = sorted((t for t in found if t.isalpha()), key=lambda t: (len(t), t))
    coding = sorted((t for t in found if not any(c.isalnum() for c in t)),
                    key=lambda t: (len(t), t))
    standard += [c for c in available(STANDARD_LIGATURES) if c not in standard]
    # Coding fonts often build ligatures from contextual rules that cannot be
    # read back as sequences, so offer the usual ones for monospaced fonts.
    monospaced = "post" in font and bool(font["post"].isFixedPitch)
    if coding or (monospaced and feature_tags & {"calt", "liga", "dlig", "clig"}):
        coding += [c for c in available(CODING_LIGATURES) if c not in coding]
    return {"standard": standard[:limit], "coding": coding[:limit]}


def feature_tags(path: str) -> frozenset[str]:
    """The GSUB feature tags of a font (empty if it has no GSUB table)."""
    font = TTFont(path, lazy=True)
    try:
        if "GSUB" not in font:
            return frozenset()
        feature_list = font["GSUB"].table.FeatureList
        return frozenset(r.FeatureTag for r in feature_list.FeatureRecord
                         ) if feature_list else frozenset()
    finally:
        font.close()


def friendly_feature_name(tag: str) -> str:
    if tag[:2] == "ss" and tag[2:].isdigit():
        return f"Stylistic Set {int(tag[2:])}"
    if tag[:2] == "cv" and tag[2:].isdigit():
        return f"Character Variant {int(tag[2:])}"
    return FEATURE_NAMES.get(tag, "")


def read_font_info(path: str) -> dict:
    """Return the GSUB features, scripts/languages and character map of a font.

    Each feature is (tag, friendly name, {glyph: replacement}) where the dict
    holds the single and alternate substitutions, the only kinds the freezer
    can bake in.
    """
    info = empty_font_info()
    font = TTFont(path, lazy=True)
    try:
        info["cmap"] = dict(font.getBestCmap() or {})
        info["ligatures"] = find_ligatures(font, info["cmap"])
        if "GSUB" not in font:
            return info
        gsub = font["GSUB"].table
        lookups = gsub.LookupList.Lookup if gsub.LookupList else []

        def substitutions(index: int) -> dict:
            pairs = {}
            for subtable in lookups[index].SubTable:
                if subtable.LookupType == 7:
                    subtable = subtable.ExtSubTable
                if subtable.LookupType == 1:
                    pairs.update(subtable.mapping)
                elif subtable.LookupType == 3:
                    pairs.update({glyph: alternates[0] for glyph, alternates
                                  in subtable.alternates.items() if alternates})
            return pairs

        features: dict[str, dict] = {}
        records = gsub.FeatureList.FeatureRecord if gsub.FeatureList else []
        for record in records:
            entry = features.setdefault(
                record.FeatureTag, {"name": "", "pairs": {}})
            for index in record.Feature.LookupListIndex:
                entry["pairs"].update(substitutions(index))
            params = getattr(record.Feature, "FeatureParams", None)
            name_id = (getattr(params, "UINameID", None)
                       or getattr(params, "FeatUILabelNameID", None))
            if name_id and not entry["name"]:
                entry["name"] = font["name"].getDebugName(name_id) or ""
        for tag, entry in features.items():
            if not entry["name"]:
                if tag[:2] == "ss" and tag[2:].isdigit():
                    entry["name"] = f"Stylistic Set {int(tag[2:])}"
                elif tag[:2] == "cv" and tag[2:].isdigit():
                    entry["name"] = f"Character Variant {int(tag[2:])}"
                else:
                    entry["name"] = FEATURE_NAMES.get(tag, "")
        # Substitutions already frozen into this font, by feature (source
        # glyphs), and glyph names that were zapped.
        encoded = encoded_glyphs(info["cmap"])
        info["zapped"] = zapped_glyphs(font)
        for tag, entry in features.items():
            frozen = {source for source, target in entry["pairs"].items()
                      if is_frozen(source, target, encoded)}
            if frozen:
                # Fully frozen when every substitution that remaps a character
                # (and whose state can be told) is frozen.
                remapping = {source for source, target in entry["pairs"].items()
                             if source in encoded or source in frozen}
                info["frozen"][tag] = {"glyphs": frozen,
                                       "full": remapping <= frozen}
        info["features"] = [
            (tag, features[tag]["name"], features[tag]["pairs"])
            for tag in sorted(features)
        ]

        if gsub.ScriptList:
            for script in gsub.ScriptList.ScriptRecord:
                info["scripts"][script.ScriptTag] = [
                    lang.LangSysTag for lang in script.Script.LangSysRecord]
    finally:
        font.close()
    return info


def main_characters(codepoints, ligatures: dict | None = None,
                    limit: int = PREVIEW_CHAR_LIMIT) -> str:
    """A sample string of the font's main encoded characters, one group per line.

    Order: A-Z, a-z, 0-9, punctuation, then sequences that may form standard
    or coding ligatures, then other Latin letters, remaining symbols, other
    scripts, and finally all letters with diacritics.
    """
    def has_diacritic(char: str) -> bool:
        return any(unicodedata.category(c) == "Mn"
                   for c in unicodedata.normalize("NFD", char)[1:])

    entries = []
    for codepoint in codepoints:
        char = chr(codepoint)
        category = unicodedata.category(char)
        if category[0] in "CZM":  # controls, spaces, combining marks
            continue
        script = ft_unicodedata.script(char)
        case = {"Lu": 0, "Ll": 1}.get(category, 2)
        if char.isascii() and char.isalpha():
            group = (0, "", case)
        elif category[0] == "L" and has_diacritic(char):
            group = (6, "", case)  # accented letters of any script come last
        elif char.isascii() and char.isdigit():
            group = (1, "", 0)
        elif char.isascii() or (category[0] == "P" and (
                codepoint < 0x250 or 0x2000 <= codepoint < 0x2070)):
            group = (2, "", 0)  # ASCII symbols and general punctuation
        elif script == "Latn":
            group = (3, "", case)
        elif script in ("Zyyy", "Zinh", "Zzzz"):  # shared by all scripts
            group = (4, "", 0 if category[0] == "N" else 1)
        else:
            group = (5, script, case)
        entries.append((*group, codepoint))

    # Other scripts follow in the order they appear in Unicode.
    first_seen: dict[str, int] = {}
    for _, script, _, codepoint in sorted(entries, key=lambda e: e[3]):
        first_seen.setdefault(script, codepoint)
    entries.sort(key=lambda e: (e[0], first_seen[e[1]], e[2], e[3]))
    # Accented letters come last but get their own allowance, so a large
    # font's other characters cannot crowd them out of the sample.
    accented = [e for e in entries if e[0] == 6][:limit // 2]
    entries = [e for e in entries if e[0] != 6][:limit] + accented

    lines: list[tuple[tuple, str]] = []
    for rank, script, kind, codepoint in entries:
        group = (rank, script, kind)
        if not lines or lines[-1][0] != group:
            lines.append((group, ""))
        lines[-1] = (group, lines[-1][1] + chr(codepoint))
    for index, kind in enumerate(("standard", "coding")):
        sequences = (ligatures or {}).get(kind)
        if sequences:  # sorts after punctuation, before accented letters
            lines.append(((2, "", 1 + index), " ".join(sequences)))
    lines.sort(key=lambda line: (line[0][0], first_seen.get(line[0][1], 0), line[0][2]))
    return "\n".join(text for _, text in lines)


# ---- sample texts for the preview ---------------------------------------- #

def western_characters(codepoints, ligatures: dict | None = None) -> str:
    """main_characters() limited to the Western and Central European
    character sets (Basic Latin, Latin-1, Latin Extended-A and the few others
    in Windows-1252 and its neighbours) with common punctuation and symbols."""
    def western(cp: int) -> bool:
        return (cp < 0x180                       # Basic Latin to Latin Extended-A
                or cp in WESTERN_EXTRAS
                or 0x2000 <= cp < 0x2090         # punctuation, super/subscript figures
                or 0x20A0 <= cp < 0x20D0         # currency
                or 0x2153 <= cp < 0x215F)        # fractions
    return main_characters([cp for cp in codepoints if western(cp)], ligatures,
                           limit=10_000)


# ƒ, Romanian Ș ș Ț ț, ẞ, the spacing accents of Windows-1252, and ℓ № ™ Ω ℮.
WESTERN_EXTRAS = frozenset((0x192, 0x218, 0x219, 0x21A, 0x21B, 0x1E9E,
                            0x2C6, 0x2C7, 0x2D8, 0x2D9, 0x2DA, 0x2DB, 0x2DC, 0x2DD,
                            0x2113, 0x2116, 0x2122, 0x2126, 0x212E))


PROSE_SAMPLE = """\
The Quick Brown Fox Jumps Over the Lazy Dog
Sphinx of black quartz, judge my vow! Pack my box with five dozen liquor jugs.

At 7:45 on a grey Tuesday in March, the ferry left Halifax with 213 passengers, \
two “mostly harmless” goats and a crate of 1,860 apples. Nobody aboard \
expected the fog — thick, quiet, and oddly fragrant — to linger past noon. \
By half past four the captain’s logbook read: “Visibility 0.5 km; spirits \
high; goats unimpressed.” The first officer, who had sailed these waters for \
nineteen years, simply shrugged and poured another coffee (black, no sugar).

Typography is the craft of making language visible. A well-set paragraph \
invites the eye to move effortlessly from word to word, line to line; a \
poorly set one makes even a fine sentence feel like work. Figures such as \
0123456789, fractions like ½ and ¾, prices of $49.99, €35 or £12, and \
percentages of 15% all deserve the same care as the letters around them.

Français : « Où est le café ? » demanda Zoé, déçue que l’hôtel fût fermé.
Deutsch: Größere Übungen fördern schöne Ergebnisse — täglich, natürlich.
Español: ¿Dónde está el niño? ¡Mañana pediremos jalapeños y piñas!
Português: A ação de graças começou às três, não às duas, irmão.
Polski: Zażółć gęślą jaźń; łódź płynie po jeziorze.
Čeština: Příliš žluťoučký kůň úpěl ďábelské ódy.
Dansk/Norsk/Svenska: Blåbærsyltetøy, smørbrød og åtte små äpplen.
"""

SAMPLE_LANGUAGES = ("JavaScript", "HTML", "CSS", "Java", "PHP", "Python", "Markdown")

CODE_SAMPLES = {
    "JavaScript": """\
// Fetch a list of fonts and keep the ones with a given feature.
const API = "https://example.com/api/fonts";

async function fontsWith(feature = "ss01") {
  const response = await fetch(`${API}?limit=50`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const fonts = await response.json();
  return fonts
    .filter((font) => font.features?.includes(feature))
    .map(({ family, weight = 400 }) => ({ family, weight }));
}

fontsWith("cv11").then((list) => {
  for (const { family, weight } of list) {
    console.log(family, weight >= 700 ? "bold" : "regular");
  }
}).catch((error) => console.error(error.message ?? error));
""",
    "HTML": """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Specimen &mdash; Frozen Features</title>
  <link rel="stylesheet" href="specimen.css">
</head>
<body>
  <!-- A simple type specimen -->
  <header class="hero">
    <h1>Aa Gg 0123456789</h1>
    <p>Stylistic sets &amp; character variants, frozen in.</p>
  </header>
  <ul id="weights">
    <li data-weight="400">Regular</li>
    <li data-weight="700"><strong>Bold</strong></li>
  </ul>
  <a href="#top" title="Back to top">&uarr; Top</a>
</body>
</html>
""",
    "CSS": """\
/* Specimen styles */
:root {
  --ink: #1e1e20;
  --paper: hsl(40 30% 97%);
  --measure: 66ch;
}

body {
  font: 400 1.125rem/1.6 "Inter", system-ui, sans-serif;
  font-feature-settings: "ss01" 1, "cv11" 1, "tnum";
  color: var(--ink);
  background: var(--paper);
  max-width: var(--measure);
  margin: 0 auto;
}

.hero h1:not(:empty)::after {
  content: " \\2192";
  letter-spacing: -0.02em;
}

@media (prefers-color-scheme: dark) {
  :root { --ink: #ececee; --paper: #202024; }
}
""",
    "Java": """\
package ca.example.fonts;

import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/** Groups font files by family name. */
public final class FontIndex {
    private static final int MAX_FILES = 1_000;

    public record Font(String family, String style, int weight) {}

    public static Map<String, List<Font>> byFamily(List<Font> fonts) {
        if (fonts == null || fonts.size() > MAX_FILES) {
            throw new IllegalArgumentException("0 < files <= " + MAX_FILES);
        }
        return fonts.stream()
                .filter(f -> f.weight() >= 100 && f.weight() <= 900)
                .collect(Collectors.groupingBy(Font::family));
    }
}
""",
    "PHP": """\
<?php
declare(strict_types=1);

// List the fonts in a folder, newest first.
function listFonts(string $folder, array $types = ['otf', 'ttf']): array
{
    $fonts = [];
    foreach (glob($folder . '/*.*') ?: [] as $path) {
        $type = strtolower(pathinfo($path, PATHINFO_EXTENSION));
        if (in_array($type, $types, true)) {
            $fonts[$path] = filemtime($path);
        }
    }
    arsort($fonts);
    return array_keys($fonts);
}

$count = count(listFonts(__DIR__ . '/fonts'));
echo "Found {$count} font" . ($count !== 1 ? 's' : '') . PHP_EOL;
""",
    "Python": """\
from pathlib import Path
from fontTools.ttLib import TTFont


def features(path: Path) -> list[str]:
    \"\"\"The GSUB feature tags of a font, sorted and without repeats.\"\"\"
    with TTFont(path, lazy=True) as font:
        if "GSUB" not in font:
            return []
        records = font["GSUB"].table.FeatureList.FeatureRecord
        return sorted({record.FeatureTag for record in records})


if __name__ == "__main__":
    for path in sorted(Path("~/Library/Fonts").expanduser().glob("*.otf")):
        tags = features(path)
        print(f"{path.name:<40} {len(tags):>3} -> {', '.join(tags[:5])}")
""",
    "Markdown": """\
# Freezing OpenType features

Some apps **ignore** OpenType features, so stylistic sets such as `ss01`
never show up. *Freezing* makes them the default.

## Steps

1. Add the fonts.
2. Tick the features to freeze — e.g. `cv11` (single-storey a).
3. Click **Generate output font files**.

> Tip: keep the originals; frozen fonts are best given a new name.

| Feature | Effect            |
|---------|-------------------|
| `tnum`  | Tabular figures   |
| `zero`  | Slashed zero      |

```bash
pyftfeatfreeze -f ss01,zero Inter.otf Inter-Frozen.otf
```

See [the freezer](https://github.com/twardoch/fonttools-opentype-feature-freezer).
""",
}

# Preview samples: key -> menu label (code samples sit in a submenu).
SAMPLE_NAMES = {
    "western": "Western character sets",
    "all": "All characters in the font",
    "prose": "Prose",
    **{f"code:{language}": f"Code: {language}" for language in SAMPLE_LANGUAGES},
    "custom": "Custom text",
}


def make_options(**overrides) -> SimpleNamespace:
    options = dict(
        inpath=None, outpath=None, features="", script=None, lang=None,
        zapnames=False, suffix=False, usesuffix="", replacenames="",
        info=False, report=False, names=False, verbose=False,
    )
    options.update(overrides)
    return SimpleNamespace(**options)


def print_report(path: str) -> None:
    """Print the scripts, languages and features of a font (pyftfeatfreeze -r)."""
    info = read_font_info(path)
    lines = []
    for script, langs in info["scripts"].items():
        lines.append(f"-s '{script}'")
        lines.extend(f"-s '{script}' -l '{lang}'" for lang in langs)
    print("# Scripts and languages:\n" + "\n".join(sorted(set(lines))))
    print("# Features:\n-f " + ",".join(tag for tag, _, _ in info["features"]))


def unique_path(folder: str, filename: str, taken: set[str]) -> str:
    """A path in `folder` that is not an input font or an earlier output."""
    stem, extension = os.path.splitext(filename)
    target = os.path.join(folder, filename)
    counter = 1
    while os.path.realpath(target) in taken:
        tail = "-featfreeze" if counter == 1 else f"-featfreeze-{counter}"
        target = os.path.join(folder, f"{stem}{tail}{extension}")
        counter += 1
    taken.add(os.path.realpath(target))
    return target


def parse_pairs(replacenames: str) -> list[tuple[str, str]]:
    """'Inter/Outer,Bold/Heavy,OT 2' -> [('Inter', 'Outer'), ('Bold', 'Heavy'),
    ('OT 2', '')]. A term without '/' (or with nothing after it) is removed.
    Items with more than one '/' or with nothing to search for are skipped."""
    pairs = []
    for pair in replacenames.split(",") if replacenames else ():
        if pair.count("/") > 1:
            continue
        search, _, replace = pair.partition("/")
        if search.strip():
            pairs.append((search, replace))
    return pairs


def valid_replacenames(replacenames: str) -> bool:
    """True when every comma-separated item is 'search/replace' or a term."""
    return all(item.count("/") <= 1 for item in replacenames.split(","))


def remove_term(text: str, term: str, compact: bool = False) -> str:
    """Remove `term` from `text` as whole words: 'Thin' goes from 'Hermes
    Thin Italic' but 'Thinner' is kept. Words in `term` may be separated by
    any spaces. With `compact`, also remove it with its spaces dropped, as in
    PostScript and file names ('OT 2' from 'HermesMaiaOT2-Thin'), where a word
    ends where the next capital, digit or '-' starts."""
    words = term.split()
    if not words:
        return text
    spaced = r"\s+".join(map(re.escape, words))
    text = re.sub(rf"(?<![^\s-]){spaced}(?![^\s-])", "", text)
    if compact:
        # 'OT 2' as 'OT2', and as 'OT-2' for names hyphenated between words.
        for short in dict.fromkeys(["".join(words), "-".join(words)]):
            lead = "" if short[0].isupper() else r"(?<![A-Za-z0-9])"
            trail = r"(?![a-z0-9])" if short[-1].isdigit() else r"(?![a-z])"
            text = re.sub(rf"{lead}{re.escape(short)}{trail}", "", text)
    return text


def apply_pairs(text: str, pairs: list[tuple[str, str]], compact: bool = False) -> str:
    """Apply the replace names pairs to `text`, in order. Case-sensitive.

    With `compact`, each pair is also applied with its spaces removed, for
    PostScript and file names, which have no spaces ('Semi Bold/Demi' also
    turns 'SemiBold' into 'Demi').

    Spaces around a search term are forgiving: if 'OT 2 ' is not found (as
    at the end of 'Hermes Maia OT 2'), 'OT 2' is tried instead. Runs of
    spaces left behind by a replacement are collapsed to one."""
    original = text
    for search, replace in pairs:
        if not replace:
            text = remove_term(text, search, compact)
            continue
        trimmed = search.strip()
        if search in text:
            text = text.replace(search, replace)
        elif trimmed and trimmed != search and trimmed in text:
            text = text.replace(trimmed, replace.strip())
        if compact:
            short = search.replace(" ", "")
            if short and short != search:
                text = text.replace(short, replace.replace(" ", ""))
    if text != original:
        text = re.sub(r" {2,}", " ", text)
        text = re.sub(r"-{2,}", "-", text)
    return text


def rename_family(family: str, pairs: list[tuple[str, str]]) -> str:
    """The family name with the replace names pairs applied, trimmed."""
    return apply_pairs(family, pairs).strip() or family


def replace_in_filename(stem: str, replacenames: str) -> str:
    """Apply the replace names pairs to a file name (without extension).

    Each pair is applied as typed and with its spaces removed, because file
    names follow PostScript names, which have no spaces. Case-sensitive.
    """
    stem = apply_pairs(stem, parse_pairs(replacenames), compact=True)
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", stem).strip()


def rename_style(full: str, postscript: str, family: str,
                 pairs: list[tuple[str, str]]) -> tuple[str, str]:
    """Apply the pairs to the style part of a full name ('Inter Bold' after
    the family 'Inter') and of a PostScript name (after the '-'), leaving the
    family part alone, since the family is renamed separately."""
    if family and full.startswith(family):
        full = (family + apply_pairs(full[len(family):], pairs)).rstrip()
    # Removals also go through the family part of the PostScript name, in
    # case it doesn't spell the family exactly as the family name does
    # ('HermesMaiaOTTwo' for 'Hermes Maia OT Two' is caught by 'OT Two', and
    # 'OTTwo' can be given as a term too). Replacements are not, since the
    # library has already applied them there.
    # Removals run over the whole PostScript name, so a term is found even
    # where the name is hyphenated differently ('HermesMaiaOT-2-Thin').
    removals = [(search, "") for search, replace in pairs if not replace]
    replacements = [(search, replace) for search, replace in pairs if replace]
    if postscript:
        had_style = "-" in postscript
        stripped = apply_pairs(postscript, removals, compact=True).strip("-")
        if not stripped or stripped.startswith(("-",)):
            stripped = postscript
        head, _, tail = stripped.partition("-")
        tail = apply_pairs(tail, replacements, compact=True).strip("-")
        # A style the pairs removed entirely ('Regular' from
        # 'Inter-Regular') leaves just the family: 'Inter'.
        postscript = f"{head}-{tail}" if had_style and tail else head
    return full, postscript


def postscript_from_names(family: str, full: str, style: str) -> str:
    """A standard 'Family-Style' PostScript name, at most 63 characters, with
    spaces and characters PostScript names can't hold removed. The style is
    the part of the full name after the family; when the full name is just
    the family ('Hermes Maia 2 Caps'), `style` is used instead. With no style
    at all, the PostScript name is the family alone. Hyphens typed into the
    family are kept ('Hermes Maia-2 Caps' -> 'HermesMaia-2Caps-Regular')."""
    def clean(text: str) -> str:
        return re.sub(r"[^!-~]|[\[\](){}<>/%]", "", text)
    tail = full[len(family):].strip() if family and full.startswith(family) else ""
    tail = clean(tail or style)
    return (f"{clean(family)}-{tail}" if tail else clean(family))[:63]


def rename_styles(font: TTFont, replacenames: str,
                  original_postscript: str | None = None) -> None:
    """Apply the replace names pairs to the style names too, after
    RemapByOTL.renameFont() has renamed the family.

    Changes the typographic style name (name ID 17), the style part of the
    full name (4), of a legacy family name like 'Inter SemiBold' (1) and of
    the PostScript names (6, 20), and the CFF names to match. Name ID 2 is
    left alone: it must stay Regular, Italic, Bold or Bold Italic for style
    linking. If the font has no ID 17, one is added with the new style name
    (and an ID 16 with the family name, which ID 17 goes with).
    """
    pairs = parse_pairs(replacenames)
    if not pairs:
        return
    name = font["name"]
    family = name.getDebugName(16) or name.getDebugName(1) or ""
    if original_postscript is None:
        original_postscript = name.getDebugName(6) or ""
    # The style as renamed, empty if the pairs removed all of it (unlike the
    # style name records below, which fall back to Regular).
    postscript_style = apply_pairs(
        name.getDebugName(17) or name.getDebugName(2) or "", pairs).strip()
    changed = False
    for record in name.names:
        text = record.toStr()
        if record.nameID == 17:
            new = apply_pairs(text, pairs).strip() or "Regular"
        elif record.nameID in (1, 4):
            new = rename_style(text, "", family, pairs)[0]
        elif record.nameID in (6, 20):
            new = rename_style("", text, family, pairs)[1]
        else:
            continue
        if new != text:
            record.string = new
            changed = True

    if name.getDebugName(17) is None:
        for record in [r for r in name.names if r.nameID == 2]:
            style = record.toStr()
            new_style = apply_pairs(style, pairs).strip() or "Regular"
            if new_style == style:
                continue
            where = (record.platformID, record.platEncID, record.langID)
            if name.getName(16, *where) is None:
                legacy = name.getName(1, *where)
                name.setName(legacy.toStr() if legacy else family, 16, *where)
            name.setName(new_style, 17, *where)
            changed = True

    # A PostScript name without a hyphen ('HermesMaiaOT2ThinItalic') can't be
    # split into family and style, so the pairs can't be applied to its style
    # part. Rebuild it from the renamed family and full names instead. This
    # goes by the original name: a hyphen the renaming put into the family
    # ('HermesMaia-2Caps…') is not a family/style separator.
    if original_postscript and "-" not in original_postscript:
        new = postscript_from_names(
            name.getDebugName(16) or name.getDebugName(1) or "",
            name.getDebugName(4) or "", postscript_style)
        for record in name.names:
            if record.nameID == 6 and record.toStr() != new:
                record.string = new
                changed = True

    if changed and "CFF " in font:
        cff = font["CFF "].cff
        full = name.getName(4, 3, 1) or name.getName(4, 1, 0)
        postscript = name.getName(6, 3, 1) or name.getName(6, 1, 0)
        if full:
            cff[0].rawDict["FullName"] = full.toStr()
        if postscript:
            cff.fontNames[0] = postscript.toStr()


def variable_filename(inpath: str, old_family: str, new_family: str,
                      replacenames: str) -> str:
    """Output name for a variable font, which covers every style and so must
    not take the default instance's style ('-Regular') from its PostScript
    name. The family part of the input file name is renamed instead:
    'GretaSansL[wght].ttf' with the family renamed from 'Greta Sans L' to
    'Greta Sans' becomes 'GretaSans[wght].ttf'."""
    source = Path(inpath)
    stem = source.stem
    old_compact = old_family.replace(" ", "")
    new_compact = new_family.replace(" ", "")
    if old_compact and old_compact in stem:
        stem = stem.replace(old_compact, new_compact, 1)
    else:
        renamed = replace_in_filename(stem, replacenames)
        if renamed != stem:
            stem = renamed
        elif new_compact:
            # Keep any axis part, as in 'Name[wght,ital]'.
            stem = new_compact + (stem[stem.index("["):] if "[" in stem else "")
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", stem).strip()
    return f"{stem or source.stem}{source.suffix}"


def output_filename(font: TTFont, inpath: str, renamed: bool,
                    replacenames: str = "") -> str:
    """Name the output after the font's new PostScript name, e.g.
    'Inter-Bold.otf' -> 'InterFrozen-Bold.otf'. The PostScript name already
    has the replace names pairs applied; without one, they're applied to the
    input file name. Unrenamed fonts keep their name."""
    source = Path(inpath)
    if renamed:
        postscript_name = font["name"].getDebugName(6) or ""
        postscript_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", postscript_name).strip()
        if postscript_name:
            return f"{postscript_name}{source.suffix}"
    stem = replace_in_filename(source.stem, replacenames) or source.stem
    return f"{stem}{source.suffix}"


def is_frozen(sub_in: str, sub_out: str, encoded) -> bool:
    """True when a substitution was already frozen into the font.

    `encoded` maps glyph names to the characters they have. Frozen means: the
    source glyph has no character, the result glyph has the character the
    source glyph's name stands for (e.g. 'a' -> 'a.1' with U+0061 on 'a.1'),
    and the source is not just a variant of the result (as in pnum's
    'space.tf' -> 'space', which is a design choice, not a frozen feature).
    """
    if sub_in in encoded or sub_out not in encoded:
        return False
    if sub_in.startswith(sub_out + "."):
        return False
    meant = agl.toUnicode(sub_in)
    return len(meant) == 1 and ord(meant) in encoded[sub_out]


def encoded_glyphs(cmap: dict) -> dict:
    """{glyph name: set of code points} from a code point -> glyph map."""
    result: dict[str, set[int]] = {}
    for codepoint, glyph in cmap.items():
        result.setdefault(glyph, set()).add(codepoint)
    return result


def remap_glyphs(remapper: RemapByOTL, choices: dict | None = None) -> None:
    """Remap the font's cmap like RemapByOTL.remapByOTL(), following the
    user's choices (see pair_wanted).

    Ticked features mean "frozen in the output". Substitutions already frozen
    into the font stay frozen when a ticked feature wants them, and are
    reversed (the original glyph gets its character back) otherwise. Other
    substitutions are frozen when a ticked feature using them wants them.
    """
    choices = choices or {}
    remapper.initSubs()
    remapper.filterFeatureIndex()
    remapper.filterLookupList()
    font = remapper.ttx
    if "GSUB" not in font:
        return  # nothing to freeze; the cmap stays as it is
    gsub = font["GSUB"].table
    lookups = gsub.LookupList.Lookup if gsub.LookupList else []
    ticked = set(remapper.filterByFeatures)
    with_unicode = {glyph for table in font["cmap"].tables
                    for glyph in table.cmap.values()}
    encoded = encoded_glyphs(font.getBestCmap() or {})
    subs = remapper.subs1

    # 1. Frozen substitutions, in any feature of any script: keep or reverse.
    holders: dict[tuple[str, str], set[str]] = {}  # frozen pair -> features
    for record in gsub.FeatureList.FeatureRecord if gsub.FeatureList else []:
        for lookup_index in record.Feature.LookupListIndex:
            for pair in lookup_pairs(lookups[lookup_index]):
                if is_frozen(*pair, encoded):
                    holders.setdefault(pair, set()).add(record.FeatureTag)
    # Reversing gives back the original glyph only for the character its
    # name stands for: the frozen glyph may also serve other characters
    # (Inter's 'a.1' is used by U+0430 and private-use code points too).
    restore: dict[int, str] = {}  # code point -> original glyph
    # A feature ticked by hand, or a glyph ticked in a list, keeps a frozen
    # substitution; failing that, an unticked feature or glyph reverses it;
    # otherwise it stays as it is in features ticked because already frozen.
    off = choices.get("off", {})
    for (sub_in, sub_out), tags in holders.items():
        wanted = any(pair_wanted(tag, sub_in, False, ticked, choices) for tag in tags)
        if not wanted:
            unwanted = any(tag not in ticked or sub_in in off.get(tag, ())
                           for tag in tags)
            wanted = not unwanted and any(
                pair_wanted(tag, sub_in, True, ticked, choices) for tag in tags)
        if not wanted:
            restore.setdefault(ord(agl.toUnicode(sub_in)), sub_in)
    restored = list(restore.values())  # run through step 2 as well

    # 2. The other substitutions of the ticked features, in lookup order.
    users: dict[int, set[str]] = {}  # lookup index -> ticked features using it
    for index in remapper.FeatureIndex:
        record = gsub.FeatureList.FeatureRecord[index]
        if record.FeatureTag in ticked:
            for lookup_index in record.Feature.LookupListIndex:
                users.setdefault(lookup_index, set()).add(record.FeatureTag)
    for lookup_index in remapper.LookupList:
        tags = users.get(lookup_index, set())
        for sub_in, sub_out in lookup_pairs(lookups[lookup_index]):
            if is_frozen(sub_in, sub_out, encoded):
                continue  # settled above
            effect = sub_in not in encoded
            if not any(pair_wanted(tag, sub_in, effect, ticked, choices)
                       for tag in tags):
                continue
            for glyphs in (subs, restored):
                for i, glyph in enumerate(glyphs):
                    if glyph == sub_in:
                        glyphs[i] = sub_out
    overrides = dict(zip(restore, restored))
    remapper.cmap_overrides = overrides

    names = []
    for sub_in, sub_out in zip(remapper.subs0, subs):
        remapper.substitution_mapping[sub_in] = sub_out
        if sub_in != sub_out:
            if sub_in not in with_unicode and sub_out not in with_unicode:
                FREEZER_LOGGER.warning(
                    "[applySubstitutions] Cannot remap '%s' -> '%s' because "
                    "neither has a Unicode value assigned in any of the cmap "
                    "tables.", sub_in, sub_out)
                continue
            names.append(sub_out)
            FREEZER_LOGGER.info("[applySubstitutions] Remap: '%s' -> '%s'",
                                sub_in, sub_out)
    remapper.remapCmaps()
    for table in font["cmap"].tables:
        for codepoint, glyph in overrides.items():
            if codepoint in table.cmap:
                table.cmap[codepoint] = glyph
    names.extend(overrides.values())
    if remapper.options.names:
        print(" ".join(names))


def output_target(folder: str, filename: str, inpath: str, taken: set[str],
                  overwrite: bool) -> str:
    """Where a font is saved. `taken` holds the input fonts and the outputs
    so far. With `overwrite`, a font may replace its own input file (but never
    another input or an earlier output); otherwise "-featfreeze" is added."""
    own = os.path.realpath(inpath)
    blocked = (taken - {own}) if overwrite else set(taken)
    target = unique_path(folder, filename, blocked)
    taken.add(os.path.realpath(target))
    return target


def library_family_rename(remapper, replacenames: str) -> None:
    """Run RemapByOTL.renameFont() with the family renamed our way.

    The library applies each pair to the family name with a plain
    str.replace, so 'OT 2 /' never matches 'Hermes Maia OT 2' (no trailing
    space) and ' OT 2/' can't be typed because the field is trimmed. Work out
    the new family with rename_family() and give the library just the one
    exact pair 'old family/new family'; the style parts, file names and other
    records are handled by rename_styles() and output_filename()."""
    name = remapper.ttx["name"]
    record = name.getName(16, 3, 1) or name.getName(1, 3, 1)
    old_family = record.toStr() if record is not None else ""
    new_family = rename_family(old_family, parse_pairs(replacenames))
    exact = ""
    if new_family != old_family and not any(c in old_family + new_family for c in "/,"):
        exact = f"{old_family}/{new_family}"
    original = remapper.options
    remapper.options = SimpleNamespace(**{**vars(original), "replacenames": exact})
    try:
        remapper.renameFont()
    finally:
        remapper.options = original


def rename_font(remapper, options: SimpleNamespace) -> str:
    """Rename the font in `remapper.ttx` as the suffix and replace names
    options say, and return its output file name. Used both when saving and
    for the names shown before saving, so the two always agree."""
    names = remapper.ttx["name"]
    old_family = names.getDebugName(16) or names.getDebugName(1) or ""
    old_postscript = names.getDebugName(6) or ""
    library_family_rename(remapper, options.replacenames)
    rename_styles(remapper.ttx, options.replacenames, old_postscript)
    renamed = bool(options.suffix or options.replacenames)
    if renamed:
        # The library leaves the unique ID (name ID 3) on the old names;
        # bring it in line with the new family and PostScript names.
        new_family = names.getDebugName(16) or names.getDebugName(1) or ""
        new_postscript = names.getDebugName(6) or ""
        for record in names.names:
            if record.nameID != 3:
                continue
            text = record.toStr()
            if old_postscript and new_postscript:
                text = text.replace(old_postscript, new_postscript)
            if old_family and new_family:
                text = text.replace(old_family, new_family)
                text = text.replace(old_family.replace(" ", ""),
                                    new_family.replace(" ", ""))
            record.string = text
    if renamed and "fvar" in remapper.ttx:
        return variable_filename(
            options.inpath, old_family,
            names.getDebugName(16) or names.getDebugName(1) or "",
            options.replacenames)
    return output_filename(remapper.ttx, options.inpath, renamed,
                           options.replacenames)


def font_names(font: TTFont) -> dict:
    """The family, style, full and PostScript names of a font, preferring the
    Windows English records and the typographic family and style."""
    name = font["name"]

    def get(name_id: int) -> str:
        record = name.getName(name_id, 3, 1, 0x409) or name.getName(name_id, 3, 1)
        return record.toStr() if record is not None else (
            name.getDebugName(name_id) or "")

    return {"family": get(16) or get(1), "style": get(17) or get(2),
            "full": get(4), "postscript": get(6)}


def freeze_font(options: SimpleNamespace, folder: str, taken: set[str],
                choices: dict | None = None, overwrite: bool = False) -> str | None:
    """Freeze one font into `folder`. Returns the saved path, or None on failure."""
    remapper = RemapByOTL(options)
    remapper.openFont()
    if not remapper.success:
        return None
    try:
        remap_glyphs(remapper, choices)
        filename = rename_font(remapper, options)
        target = output_target(folder, filename, options.inpath, taken, overwrite)
        replacing = os.path.realpath(target) == os.path.realpath(options.inpath)
        # The input is still being read, so never write over it directly:
        # save next to it and swap the files once it is closed.
        remapper.outpath = target + ".featfreeze-tmp" if replacing else target
        remapper.saveFont()
        saved = remapper.success
    finally:
        remapper.closeFont()
    if not saved:
        if os.path.exists(remapper.outpath) and remapper.outpath != target:
            os.remove(remapper.outpath)
        return None
    if remapper.outpath != target:
        os.replace(remapper.outpath, target)
    return target


def predict_names(path: str, suffix: str, replacenames: str) -> dict:
    """The names a font will get, without freezing anything.

    Runs the same renaming as saving does (rename_font) on the font in
    memory. Returns a dict with 'filename', 'family', 'style', 'full' and
    'postscript'.
    """
    font = TTFont(path, lazy=True)
    try:
        if not suffix and not replacenames:
            return {"filename": os.path.basename(path), **font_names(font)}
        name = font["name"]
        if name.getName(16, 3, 1) is None and name.getName(1, 3, 1) is None:
            raise ValueError("no Windows family name to rename")
        options = make_options(inpath=path, suffix=bool(suffix),
                               usesuffix=suffix, replacenames=replacenames)
        remapper = RemapByOTL(options)
        remapper.ttx = font
        filename = rename_font(remapper, options)
        return {"filename": filename, **font_names(font)}
    finally:
        font.close()


def build_preview_font(path: str, features: str, script, lang, family: str,
                       choices: dict | None = None):
    """Freeze `path` in memory under a unique family name.

    Returns (font bytes, set of code points whose glyph changed).
    """
    remapper = RemapByOTL(make_options(
        inpath=path, outpath=os.devnull, features=features,
        script=script, lang=lang))
    remapper.openFont()
    if not remapper.success:
        raise RuntimeError(f"Cannot open {path}")
    try:
        font = remapper.ttx
        before = dict(font.getBestCmap() or {})
        remap_glyphs(remapper, choices)
        mapping = remapper.substitution_mapping
        overrides = getattr(remapper, "cmap_overrides", {})
        changed = {cp for cp, glyph in before.items()
                   if overrides.get(cp, mapping.get(glyph, glyph)) != glyph}

        # A unique family name stops the OS from substituting an installed copy.
        name = font["name"]
        name.names = []
        for name_id, value in ((1, family), (2, "Regular"), (4, family), (6, family)):
            name.setName(value, name_id, 3, 1, 0x409)
            name.setName(value, name_id, 1, 0, 0)
        if "CFF " in font:
            cff = font["CFF "].cff
            cff.fontNames[0] = family
            cff[0].rawDict["FamilyName"] = family
            cff[0].rawDict["FullName"] = family
        font.flavor = None
        buffer = io.BytesIO()
        font.save(buffer)
        return buffer.getvalue(), changed
    finally:
        remapper.closeFont()


# --------------------------------------------------------------------------- #
# Background worker
# --------------------------------------------------------------------------- #

class _LineStream(io.TextIOBase):
    """File-like object that forwards complete lines to a callback."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback
        self._pending = ""

    def write(self, text: str) -> int:
        self._pending += text
        *lines, self._pending = self._pending.split("\n")
        for line in lines:
            self._callback(line)
        return len(text)

    def flush(self) -> None:
        if self._pending:
            self._callback(self._pending)
            self._pending = ""


class _CallbackHandler(logging.Handler):
    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        self._callback(f"{record.levelname}: {record.getMessage()}")


class FreezeWorker(QThread):
    """Loops through the input fonts and writes the processed copies."""

    message = Signal(str)
    progress = Signal(int)
    completed = Signal(int, int)  # saved, failed

    def __init__(self, paths: list[str], folder: str, settings: dict,
                 overwrite: bool = False, parent=None):
        super().__init__(parent)
        self._overwrite = overwrite
        self._paths = paths
        self._folder = folder
        self._settings = settings

    def run(self) -> None:
        settings = self._settings
        stream = _LineStream(self.message.emit)
        handler = _CallbackHandler(self.message.emit)
        FREEZER_LOGGER.addHandler(handler)
        FREEZER_LOGGER.setLevel(
            logging.INFO if settings["verbose"] else logging.WARNING)
        # Outputs never overwrite another input font or each other; a font
        # replaces its own file only when the user chose to overwrite.
        taken = {os.path.realpath(path) for path in self._paths}
        saved = failed = 0
        try:
            with contextlib.redirect_stdout(stream):
                for index, inpath in enumerate(self._paths, start=1):
                    print(f"[{index}/{len(self._paths)}] {inpath}")
                    outpath = None
                    try:
                        if settings["report"]:
                            print_report(inpath)
                        if settings["names"]:
                            print("# Remapped glyph names:")
                        options = make_options(inpath=inpath, **{
                            key: settings[key] for key in (
                                "features", "script", "lang", "zapnames",
                                "suffix", "usesuffix", "replacenames",
                                "info", "names", "verbose")})
                        outpath = freeze_font(options, self._folder, taken,
                                              settings["choices"], self._overwrite)
                    except Exception as exc:  # keep going with the other fonts
                        print(f"ERROR: {type(exc).__name__}: {exc}")
                    if outpath:
                        saved += 1
                        print(f"Saved: {outpath}")
                    else:
                        failed += 1
                        print(f"FAILED: {inpath}")
                    print("")
                    stream.flush()
                    self.progress.emit(index)
        finally:
            FREEZER_LOGGER.removeHandler(handler)
            FREEZER_LOGGER.setLevel(logging.ERROR)
        self.completed.emit(saved, failed)


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

def _label(text: str, role: str, indent: int = 0) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", role)
    label.setWordWrap(True)
    if indent:
        label.setContentsMargins(indent, 0, 0, 0)
    return label


class PreviewEdit(QTextEdit):
    """Sample text box that colours changed characters glyph by glyph.

    Qt's own highlighting (extra selections) colours a strip as wide as each
    character, which cuts slanted italics and ligatures in two and tints the
    overhang of unchanged neighbours. Formatting the text instead makes Qt
    reshape at every colour change, losing kerning and ligatures. So the text
    is drawn as usual and each glyph that shows a changed character is then
    painted again, whole, in the highlight colour."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._ranges: list[tuple[int, int]] = []  # [start, end) document positions
        self._color = QColor()
        self._background = QColor()

    def set_highlight(self, ranges, color: QColor, background: QColor) -> None:
        self._ranges = list(ranges)
        self._color = QColor(color)
        self._background = QColor(background)
        self.viewport().update()

    def _unselected(self) -> list[tuple[int, int]]:
        """Highlight ranges minus the user's selection (drawn by Qt)."""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return self._ranges
        low, high = cursor.selectionStart(), cursor.selectionEnd()
        pieces = []
        for start, end in self._ranges:
            if start < low:
                pieces.append((start, min(end, low)))
            if end > high:
                pieces.append((max(start, high), end))
        return pieces

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        ranges = self._unselected()
        if not ranges:
            return
        document = self.document()
        scroll = QPointF(self.horizontalScrollBar().value(),
                         self.verticalScrollBar().value())
        visible = QRectF(event.rect()).translated(scroll)
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for start, end in ranges:
            block = document.findBlock(start)
            while block.isValid() and block.position() < end:
                layout = block.layout()
                origin = layout.position()
                if layout.boundingRect().translated(origin).intersects(visible):
                    first = max(start, block.position()) - block.position()
                    last = min(end, block.position() + block.length()) - block.position()
                    runs = []
                    for index in range(layout.lineCount()):
                        line = layout.lineAt(index)
                        low = max(first, line.textStart())
                        high = min(last, line.textStart() + line.textLength())
                        if high > low:
                            runs += line.glyphRuns(low, high - low)
                    at = origin - scroll
                    # Clear the glyphs first so no dark edge shows round the
                    # colour, then paint them in the highlight colour.
                    for color in (self._background, self._background, self._color):
                        painter.setPen(color)
                        for run in runs:
                            painter.drawGlyphRun(at, run)
                block = block.next()
        painter.end()


class NameLabel(QLabel):
    """Selectable font name that wraps inside a narrow column.

    Font names often have no spaces (PostScript names never do), so invisible
    break points are added after hyphens and underscores and between a lower-
    case letter and a capital. Copying gives back the name without them."""

    BREAK = "\u200b"  # zero-width space

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        self.setSizePolicy(policy)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt naming)
        self._plain = text
        shown = re.sub(r"([-_./])", r"\1" + self.BREAK, text)
        shown = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", self.BREAK, shown)
        super().setText(shown)

    def plain_text(self) -> str:
        return self._plain

    def _copy(self) -> None:
        text = self.selectedText() or self.text()
        QApplication.clipboard().setText(text.replace(self.BREAK, ""))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.matches(QKeySequence.StandardKey.Copy):
            self._copy()
        else:
            super().keyPressEvent(event)

    def _menu(self, point) -> None:
        menu = QMenu(self)
        menu.addAction("Copy", self._copy)
        menu.exec(self.mapToGlobal(point))


class ElidingCheckBox(QCheckBox):
    """A checkbox whose label is shortened with '…' to fit its width; when
    shortened, the full label is added to its tooltip."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._full_text = text
        self._base_tip = ""
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        self.setSizePolicy(policy)
        self.setMinimumWidth(40)

    def setToolTip(self, tip: str) -> None:  # noqa: N802 (Qt naming)
        self._base_tip = tip
        self._fit()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (event.Type.FontChange, event.Type.StyleChange):
            self._fit()

    def _fit(self) -> None:
        metrics = self.fontMetrics()
        # Width taken by the box itself and its spacing.
        overhead = super().sizeHint().width() - metrics.horizontalAdvance(super().text())
        room = max(10, self.width() - overhead - 2)
        shown = metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, room)
        if shown != super().text():
            super().setText(shown)
        tip = self._base_tip
        if shown != self._full_text:
            tip = self._full_text + ("\n\n" + tip if tip else "")
        super().setToolTip(tip)


class FontTable(QTreeWidget):
    """Input fonts (column 0) and their output file names (column 1).

    Offers the list-style methods the window uses (count, item, row, ...).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(2)
        self.setHeaderLabels(["Font file", "Output file"])
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAllColumnsShowFocus(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionsMovable(False)

    def count(self) -> int:
        return self.topLevelItemCount()

    def item(self, row: int):
        return self.topLevelItem(row)

    def row(self, item) -> int:
        return self.indexOfTopLevelItem(item)

    def takeItem(self, row: int):
        return self.takeTopLevelItem(row)

    def addItem(self, item) -> None:
        self.addTopLevelItem(item)

    def setCurrentRow(self, row: int) -> None:
        self.setCurrentItem(self.topLevelItem(row))


class HelpButton(QToolButton):
    """A (?) button that shows its help text in a small popup when clicked."""

    instances: list["HelpButton"] = []

    def __init__(self, text: str, name: str = "", parent=None):
        super().__init__(parent)
        self.help_text = text
        self.setObjectName("help")
        self.setFixedSize(18, 18)
        self.setIconSize(QSize(16, 16))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"Help: {name}" if name else "Help")
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.clicked.connect(self.show_popup)
        self._popup = None
        HelpButton.instances.append(self)

    def show_popup(self) -> None:
        if self._popup is not None:
            self._popup.deleteLater()
        popup = QFrame(self, Qt.WindowType.Popup)
        popup.setObjectName("helpPopup")
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(12, 10, 12, 10)
        label = QLabel(self.help_text)
        label.setWordWrap(True)
        label.setFixedWidth(320)
        layout.addWidget(label)
        popup.adjustSize()
        position = self.mapToGlobal(QPoint(0, self.height() + 4))
        screen = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        position.setX(min(position.x(), screen.right() - popup.width() - 8))
        if position.y() + popup.height() > screen.bottom():
            position.setY(self.mapToGlobal(QPoint(0, 0)).y() - popup.height() - 4)
        popup.move(position)
        popup.show()
        self._popup = popup


class GlyphPainter:
    """Draws glyphs of one font as small pictures."""

    def __init__(self, path: str):
        self.font = TTFont(path, lazy=True)
        self.glyphs = self.font.getGlyphSet()
        hhea = self.font["hhea"]
        self.ascent, self.descent = hhea.ascent, hhea.descent

    def close(self) -> None:
        self.font.close()

    def pixmap(self, name: str, height: int, color: QColor, ratio: float = 2.0) -> QPixmap:
        """The glyph on a transparent pixmap `height` points tall."""
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        width = height
        if name in self.glyphs:
            glyph = self.glyphs[name]
            glyph.draw(QtPen(self.glyphs, path))
            em = max(self.ascent - self.descent, 1)
            scale = height / em
            width = max(height, int(glyph.width * scale) + 4)
            transform = QTransform()
            transform.translate((width - glyph.width * scale) / 2, self.ascent * scale)
            transform.scale(scale, -scale)
            path = transform.map(path)
        pixmap = QPixmap(int(width * ratio), int(height * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillPath(path, color)
        painter.end()
        return pixmap


class RichItemDelegate(QStyledItemDelegate):
    """Draws list items that carry HTML (in HTML_ROLE) as rich text, so part
    of an item can be struck through; other items are drawn as usual."""

    HTML_ROLE = Qt.ItemDataRole.UserRole + 1

    def paint(self, painter, option, index) -> None:
        markup = index.data(self.HTML_ROLE)
        if not markup:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        opt.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        # The application palette holds the theme's colours (a style sheet
        # may hide them from the option's palette).
        enabled = bool(index.flags() & Qt.ItemFlag.ItemIsEnabled)
        group = (QPalette.ColorGroup.Normal if enabled
                 else QPalette.ColorGroup.Disabled)
        role = (QPalette.ColorRole.HighlightedText
                if enabled and opt.state & QStyle.StateFlag.State_Selected
                else QPalette.ColorRole.Text)
        color = QApplication.palette().color(group, role).name()
        document = QTextDocument()
        document.setDefaultFont(opt.font)
        document.setDocumentMargin(0)
        document.setHtml(f'<span style="color:{color}; white-space:pre">{markup}</span>')
        painter.save()
        painter.setClipRect(text_rect)
        painter.translate(text_rect.left() + 3, text_rect.top()
                          + (text_rect.height() - document.size().height()) / 2)
        document.drawContents(painter)
        painter.restore()


class SubstitutionDialog(QDialog):
    """Lists one feature's glyph substitutions with a checkbox each: ticked
    means frozen in the output fonts."""

    GLYPH_SIZE = 34
    ZAPPED = "(glyph prev. zapped)"
    changed = Signal(object)  # {source glyph: ticked}, on every change

    def __init__(self, parent, font_path: str, tag: str, name: str,
                 pairs: dict, cmap: dict, states: dict, zapped: set[str],
                 frozen: set[str]):
        super().__init__(parent)
        self.setWindowTitle(f"{tag} substitutions")
        self.resize(600, 640)
        layout = QVBoxLayout(self)
        layout.addWidget(_label(f"{tag}  ·  {name}" if name else tag, "title"))
        hint = (f"Substitutions in {os.path.basename(font_path)}. Ticked "
                "glyphs are frozen in all output fonts; untick one to leave "
                "it unchanged, or, if it was already frozen, to give the "
                "character its original glyph back.")
        if any(state is None for state in states.values()):
            hint += (" Greyed-out substitutions involve zapped glyph names, "
                     "which hide whether they were frozen, so they are left "
                     "as they are.")
        layout.addWidget(_label(hint, "hint"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by glyph name or character")
        self.filter_edit.textChanged.connect(self._filter)
        layout.addWidget(self.filter_edit)

        self.list = QListWidget()
        size = self.GLYPH_SIZE
        self.list.setIconSize(QSize(size * 3, size))
        self.list.setUniformItemSizes(True)
        self.list.setItemDelegate(RichItemDelegate(self.list))
        layout.addWidget(self.list, 1)
        char_of = {}
        for codepoint in sorted(cmap, reverse=True):
            char_of[cmap[codepoint]] = codepoint
        color = self.palette().color(QPalette.ColorRole.Text)
        painter = GlyphPainter(font_path)

        def shown(glyph: str, rich: bool) -> str:
            if glyph not in zapped:
                return html.escape(glyph) if rich else glyph
            return (f"<s>{html.escape(glyph)}</s> {self.ZAPPED}" if rich
                    else f"{glyph} {self.ZAPPED}")

        try:
            for source, target in pairs.items():
                extra = ""
                if source in frozen:  # its character went to the frozen glyph
                    codepoint = ord(agl.toUnicode(source))
                    extra += f"     {chr(codepoint)}  U+{codepoint:04X}"
                elif source in char_of:
                    extra += f"     {chr(char_of[source])}  U+{char_of[source]:04X}"
                if source in frozen:
                    extra += "     already frozen"
                text = f"{shown(source, False)}  →  {shown(target, False)}{extra}"
                state = states.get(source, True)
                ink = color if state is not None else QApplication.palette().color(
                    QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
                item = QListWidgetItem(QIcon(self._pair_picture(
                    painter, source, target, ink)), text)
                item.setData(Qt.ItemDataRole.UserRole, source)
                if source in zapped or target in zapped:
                    item.setData(RichItemDelegate.HTML_ROLE,
                                 f"{shown(source, True)}  →  {shown(target, True)}"
                                 f"{html.escape(extra)}")
                if state is None:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled
                                  & ~Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.PartiallyChecked)
                    item.setToolTip("Zapped glyph names hide whether this "
                                    "substitution was already frozen, so it "
                                    "is left as it is.")
                else:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked if state
                                       else Qt.CheckState.Unchecked)
                self.list.addItem(item)
        finally:
            painter.close()

        row = QHBoxLayout()
        for text, state in (("Enable all", True), ("Disable all", False)):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, s=state: self._set_visible(s))
            row.addWidget(button)
        self.count_label = _label("", "hint")
        row.addWidget(self.count_label, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)
        self.list.itemChanged.connect(self._update_count)
        self.list.itemChanged.connect(lambda *_: self.changed.emit(self.states()))
        self._update_count()

    def _pair_picture(self, painter: GlyphPainter, source: str, target: str,
                      color: QColor) -> QPixmap:
        size, ratio = self.GLYPH_SIZE, 2.0
        before = painter.pixmap(source, size, color, ratio)
        after = painter.pixmap(target, size, color, ratio)
        width = size * 3
        picture = QPixmap(int(width * ratio), int(size * ratio))
        picture.setDevicePixelRatio(ratio)
        picture.fill(Qt.GlobalColor.transparent)
        p = QPainter(picture)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        third = width / 3
        for pixmap, centre in ((before, third / 2), (after, width - third / 2)):
            w = min(pixmap.width() / ratio, third)
            p.drawPixmap(int(centre - w / 2), 0, int(w), size, pixmap)
        arrow = QColor(color)
        arrow.setAlphaF(0.45)
        p.setPen(arrow)
        p.drawText(int(third), 0, int(third), size, Qt.AlignmentFlag.AlignCenter, "→")
        p.end()
        return picture

    def _items(self):
        return [self.list.item(i) for i in range(self.list.count())]

    def _filter(self, text: str) -> None:
        text = text.strip().lower()
        for item in self._items():
            item.setHidden(bool(text) and text not in item.text().lower())

    def _changeable(self, item) -> bool:
        return bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def _set_visible(self, enabled: bool) -> None:
        state = Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
        self.list.blockSignals(True)  # one update for the lot
        for item in self._items():
            if not item.isHidden() and self._changeable(item):
                item.setCheckState(state)
        self.list.blockSignals(False)
        self._update_count()
        self.changed.emit(self.states())

    def _update_count(self, *_args) -> None:
        on = sum(item.checkState() == Qt.CheckState.Checked for item in self._items())
        fixed = sum(not self._changeable(item) for item in self._items())
        text = f"{on} of {self.list.count()} frozen"
        if fixed:
            text += f", {fixed} left as they are"
        self.count_label.setText(text)

    def states(self) -> dict:
        """{source glyph: ticked} for the substitutions that can be changed."""
        return {item.data(Qt.ItemDataRole.UserRole):
                item.checkState() == Qt.CheckState.Checked
                for item in self._items() if self._changeable(item)}


def _with_help(widget: QWidget, text: str, name: str = "") -> QWidget:
    """`widget` followed by a (?) help button."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    layout.addWidget(widget)
    layout.addWidget(HelpButton(text, name), 0, Qt.AlignmentFlag.AlignVCenter)
    layout.addStretch(1)
    return row


def _section(title: str, help_text: str = "") -> tuple[QWidget, QVBoxLayout, QLabel]:
    """A pane: a large title (with an optional (?) button) above its contents."""
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    title_label = _label(title, "title")
    title_label.setWordWrap(False)
    layout.addWidget(_with_help(title_label, help_text, title) if help_text
                     else title_label)
    return widget, layout, title_label


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OpenType Feature Freezer")
        self.resize(1480, 920)
        self.setMinimumWidth(1100)

        self._active_font: str | None = None   # the font shown in features/preview
        self._font_info = empty_font_info()
        self._selected_tags: set[str] = set()  # ticked features, kept across fonts
        # Choices in the substitution lists (see pair_wanted): "off"/"on" map a
        # tag to source glyphs unticked/ticked; features in "keep" (ticked
        # because they were already frozen) leave untouched glyphs as they are.
        self._choices: dict = {"off": {}, "on": {}, "keep": set()}
        self._frozen_cache: dict[str, set[str]] = {}  # path -> frozen features
        self._previewing_tag: str | None = None   # feature whose dialog is open
        self._last_font_folder = ""                # where "Add fonts…" last looked
        self._status_text = ("", "")               # preview status: before / after count
        self._auto_preview_text = ""           # last preview text the app filled in
        self._filling_preview = False          # True while the app sets the text
        self._auto_output = ""                 # output folder filled in by the app
        self._feature_boxes: list[QCheckBox] = []
        self._changed: set[int] = set()
        self._preview_font_id = -1
        self._preview_family = ""
        self._preview_counter = 0
        self._worker: FreezeWorker | None = None
        self._tag_cache: dict[str, frozenset[str]] = {}  # feature tags per font
        self._listed_fonts: tuple[str, ...] = ()

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(200)
        self._preview_timer.timeout.connect(self._update_preview)

        self._settings = QSettings("featfreeze_gui", "featfreeze_gui")
        app = QApplication.instance()
        app.setStyle("Fusion")  # the one style that follows a custom palette everywhere
        # Start in the system's appearance every time, and follow it when it
        # changes; the theme button overrides it until the system changes again.
        # Follow the system unless the theme button has set an override,
        # which is remembered across launches.
        override = self._settings.value("theme_override", "")
        self._theme_override = override if override in THEMES else ""
        self._theme = self._theme_override or system_theme(app)
        # The preview's sample text, and the user's own text, kept across
        # launches; the custom text starts empty.
        sample = self._settings.value("sample", "western")
        self._sample = sample if sample in SAMPLE_NAMES else "western"
        self._custom_text = str(self._settings.value("custom_text", "") or "")
        try:
            app.styleHints().colorSchemeChanged.connect(self._system_theme_changed)
        except AttributeError:  # Qt older than 6.5
            pass

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        layout.addLayout(self._build_header())
        # Three columns (the third a little wider):
        #   columns 1-2, top:   font files to process (with output names)
        #   column 1, below:    names of the selected font, then options
        #   column 2, below:    OpenType features
        #   column 3:           preview and processing output
        files = self._build_files_side()
        names = self._build_names()
        options = self._build_options()
        features = self._build_features()
        output_side = self._build_output_side()
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(16)
        grid.addWidget(files, 0, 0, 1, 2)
        grid.addWidget(names, 1, 0)
        grid.addWidget(options, 2, 0)
        grid.addWidget(features, 1, 1, 2, 1)
        grid.addWidget(output_side, 0, 2, 3, 1)
        # Columns 1 and 2 share one width; column 3 (preview) is a quarter wider.
        for column, stretch in enumerate((4, 4, 5)):
            grid.setColumnStretch(column, stretch)
        grid.setRowStretch(0, 2)
        grid.setRowStretch(2, 3)
        # Ignore content widths so the columns stay equal at any window width.
        for widget in (files, names, options, features, output_side):
            policy = widget.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            widget.setSizePolicy(policy)
        self._control_panels = [files, features, options]
        # Wrapped names grow taller as the column narrows; Qt's minimum sizes
        # ignore that, so the names and options would get squeezed together.
        self._names_panel, self._options_panel = names, options
        names.installEventFilter(self)
        layout.addWidget(body, 1)
        self.setCentralWidget(central)

        # Fonts can be dropped anywhere on the window. Text boxes would
        # otherwise take the drop themselves and insert the path as text.
        self.setAcceptDrops(True)
        for kind in (QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox):
            for widget in self.findChildren(kind):
                widget.setAcceptDrops(False)
                if isinstance(widget, (QTextEdit, QPlainTextEdit)):
                    widget.viewport().setAcceptDrops(False)

        # Keep the predicted output names current.
        self._names_timer = QTimer(self)
        self._names_timer.setSingleShot(True)
        self._names_timer.setInterval(250)
        self._names_timer.timeout.connect(self._update_output_names)
        for signal in (self.suffix_edit.textChanged,
                       self.replacenames_edit.textChanged,
                       self.output_edit.textChanged):
            signal.connect(self._names_timer.start)

        self._set_theme(self._theme)
        self._update_file_count()
        self._rebuild_features()
        self._show_sample()

    # ---- construction ---------------------------------------------------- #

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(_with_help(_label("OpenType Feature Freezer", "appTitle"),
                                  APP_DESCRIPTION, "About"))
        row.addLayout(text, 1)
        self.theme_button = QPushButton()
        self.theme_button.setObjectName("theme")
        self.theme_button.setFixedSize(28, 28)
        self.theme_button.setIconSize(QSize(16, 16))
        self.theme_button.clicked.connect(self._toggle_theme)
        row.addWidget(self.theme_button, 0, Qt.AlignmentFlag.AlignTop)
        return row

    def _build_files_side(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Font files to process, each with its output file name
        section, box, self.files_title = _section("Font files to process",
                                                  POPUP["inpath"])
        self.font_list = FontTable()
        # Keep the count in the title up to date however rows come and go.
        model = self.font_list.model()
        for signal in (model.rowsInserted, model.rowsRemoved, model.modelReset):
            signal.connect(self._update_file_count)
        self.font_list.setMinimumHeight(110)
        self.font_list.itemSelectionChanged.connect(self._fonts_changed)
        box.addWidget(self.font_list, 1)
        row = QHBoxLayout()
        add_button = QPushButton("Add fonts…")
        add_button.clicked.connect(self._add_fonts)
        remove_button = QPushButton("Remove selected")
        remove_button.clicked.connect(self._remove_fonts)
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self._clear_fonts)
        for button in (add_button, remove_button, clear_button):
            row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)
        layout.addWidget(section, 1)

        # Output folder, on one line under the list
        row = QHBoxLayout()
        row.addWidget(_with_help(QLabel("Output folder"), POPUP["output"],
                                 "Output folder"))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Folder for the processed font files")
        browse_button = QPushButton("Choose…")
        browse_button.clicked.connect(self._choose_output)
        row.addWidget(self.output_edit, 1)
        row.addWidget(browse_button)
        layout.addLayout(row)
        return panel

    def _build_features(self) -> QWidget:
        section, box, self.features_title = _section(
            "OpenType features", POPUP["features"])
        self.features_area = QScrollArea()
        self.features_area.setWidgetResizable(True)
        self.features_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.features_area.setMinimumHeight(150)
        box.addWidget(self.features_area, 1)
        self.features_extra = _label("", "hint")
        self.features_extra.setVisible(False)
        box.addWidget(self.features_extra)
        row = QHBoxLayout()
        all_button = QPushButton("Select all")
        all_button.clicked.connect(lambda: self._set_all_features(True))
        none_button = QPushButton("Select none")
        none_button.clicked.connect(lambda: self._set_all_features(False))
        reset_button = QPushButton("Reset to input font")
        reset_button.setToolTip(
            "Tick only the features already frozen into the input fonts and "
            "undo all changes made in the substitution lists, so the output "
            "fonts keep the input fonts' glyphs.")
        reset_button.clicked.connect(self._reset_features)
        row.addWidget(all_button)
        row.addWidget(none_button)
        row.addWidget(reset_button)
        row.addStretch(1)
        box.addLayout(row)
        return section

    def _build_names(self) -> QWidget:
        """Input and output names of the selected font, in a small table."""
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(_label("Input and output names for selected font", "title"))
        names = QGridLayout()
        names.setHorizontalSpacing(12)
        names.setVerticalSpacing(4)
        names.setColumnStretch(1, 1)
        names.setColumnStretch(2, 1)
        for col, text in ((1, "Input"), (2, "Output")):
            names.addWidget(_label(text, "subtitle"), 0, col)
        self._name_labels = {}
        for row, (key, text) in enumerate((("family", "Family"),
                                           ("style", "Style"),
                                           ("full", "Full"),
                                           ("postscript", "PostScript")), start=1):
            label = _label(text, "hint")
            names.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
            for col, side in ((1, "input"), (2, "output")):
                value = NameLabel("–")
                value.setToolTip("Before processing" if side == "input"
                                 else "After processing")
                names.addWidget(value, row, col, Qt.AlignmentFlag.AlignTop)
                self._name_labels[(key, side)] = value
        column.addLayout(names)
        return panel

    def _build_options(self) -> QWidget:
        """Options column: freezing and reporting at the top, renaming and the
        PostScript name of the selected font at the bottom."""
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(_label("Options", "title"))

        def block() -> QGridLayout:
            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setColumnStretch(1, 1)
            grid.setVerticalSpacing(4)
            return grid

        # One grid for all three groups, so the gaps between them match.
        grid = block()

        def add_heading(text: str) -> None:
            row = grid.rowCount()
            if row > 1:
                grid.setRowMinimumHeight(row, 10)
                row += 1
            heading = _label(text, "subtitle")
            heading.setContentsMargins(0, 0, 0, 8)  # room above the first option
            grid.addWidget(heading, row, 0, 1, 2)

        def add_field(name: str, widget: QWidget, key: str) -> None:
            row = grid.rowCount()
            grid.addWidget(_with_help(QLabel(name), POPUP[key], name), row, 0)
            grid.addWidget(widget, row, 1)
            grid.setRowMinimumHeight(row + 1, 6)

        def add_check(key: str) -> QCheckBox:
            # Every checkbox starts in column 0, so they all line up.
            check = QCheckBox(LABELS[key])
            row = grid.rowCount()
            grid.addWidget(_with_help(check, POPUP[key], LABELS[key]), row, 0, 1, 2)
            grid.setRowMinimumHeight(row + 1, 6)
            return check

        add_heading(GROUP_RENAMING)
        self.suffix_edit = QLineEdit()
        self.suffix_edit.setPlaceholderText("e.g. SC (empty: no suffix)")
        add_field(LABELS["usesuffix"], self.suffix_edit, "usesuffix")
        self.replacenames_edit = QLineEdit()
        self.replacenames_edit.setPlaceholderText("search/replace, or a word to remove")
        add_field(LABELS["replacenames"], self.replacenames_edit, "replacenames")
        self.info_check = add_check("info")

        add_heading(GROUP_FREEZING)
        self.script_combo = QComboBox()
        self.script_combo.setEditable(True)
        self.script_combo.currentTextChanged.connect(self._script_changed)
        add_field("script", self.script_combo, "script")
        self.lang_combo = QComboBox()
        self.lang_combo.setEditable(True)
        self.lang_combo.currentTextChanged.connect(self._schedule_preview)
        add_field("language", self.lang_combo, "lang")
        self.zapnames_check = add_check("zapnames")

        add_heading(GROUP_REPORTING)
        self.report_check = add_check("report")
        self.names_check = add_check("names")
        self.verbose_check = add_check("verbose")

        column.addLayout(grid)
        column.addStretch(1)
        return panel

    def _build_output_side(self) -> QWidget:
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setHandleWidth(14)

        section, box, preview_title = _section("Preview")
        row = QHBoxLayout()
        self._hide_unchanged = False
        self.unchanged_button = QPushButton("Hide unchanged characters")
        self.unchanged_button.setToolTip(
            "Switch between the main characters of the selected font and only "
            "the characters the selected features change.")
        self.unchanged_button.clicked.connect(self._toggle_unchanged)
        self.sample_button = QPushButton()
        self.sample_button.setToolTip("Choose the sample text shown in the preview")
        menu = QMenu(self.sample_button)
        code_menu = None
        for key, label in SAMPLE_NAMES.items():
            if key.startswith("code:"):
                if code_menu is None:
                    code_menu = menu.addMenu("Code")
                target = code_menu
            else:
                if key == "custom":
                    menu.addSeparator()
                target = menu
            action = target.addAction(label.removeprefix("Code: "))
            action.triggered.connect(lambda _=False, k=key: self._choose_sample(k))
        self.sample_button.setMenu(menu)
        self._label_sample_button()
        self.size_spin = QSpinBox()
        self.size_spin.setRange(8, 200)
        self.size_spin.setValue(32)
        self.size_spin.setSuffix(" pt")
        self.size_spin.valueChanged.connect(self._apply_preview_font)
        # The size sits on the title line, leaving the row below to the buttons.
        box.removeWidget(preview_title)
        title_row = QHBoxLayout()
        title_row.addWidget(preview_title)
        title_row.addStretch(1)
        title_row.addWidget(QLabel("Size"))
        title_row.addWidget(self.size_spin)
        box.addLayout(title_row)
        row.addWidget(self.unchanged_button)
        row.addWidget(self.sample_button)
        row.addStretch(1)
        box.addLayout(row)
        self.preview_edit = PreviewEdit()
        self.preview_edit.setAcceptRichText(False)
        self.preview_edit.setPlaceholderText(
            "Add a font to see a preview. You can also type your own text here.")
        self.preview_edit.textChanged.connect(self._refresh_changed_highlight)
        self.preview_edit.textChanged.connect(self._keep_custom_text)
        self.preview_edit.selectionChanged.connect(self.preview_edit.viewport().update)
        box.addWidget(self.preview_edit, 1)
        self.preview_status = _label("", "hint")
        box.addWidget(self.preview_status)
        box.addWidget(_label("Note that some changed characters may not be "
                             "present in the above sample text.", "hint"))
        splitter.addWidget(section)

        section, box, _ = _section("Processing output (showing progress)")
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        box.addWidget(self.log_edit, 1)
        row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.generate_button = QPushButton("Generate output font files")
        self.generate_button.setObjectName("primary")
        self.generate_button.clicked.connect(self._generate)
        row.addWidget(self.progress_bar, 1)
        row.addStretch(0)
        row.addWidget(self.generate_button)
        box.addLayout(row)
        splitter.addWidget(section)

        splitter.setSizes([640, 170])
        return splitter

    # ---- appearance ------------------------------------------------------ #

    def _set_theme(self, name: str) -> None:
        self._theme = name
        apply_theme(QApplication.instance(), name)
        # A new style sheet resets widget fonts, so set them again.
        self._apply_preview_font()
        if hasattr(self, "_status_text"):
            self._refresh_changed_highlight()  # blue for this theme
        self.log_edit.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        # The icon shows the mode the button switches to.
        other = "light" if name == "dark" else "dark"
        icon = theme_icons(name)["sun" if name == "dark" else "moon"]
        self.theme_button.setIcon(QIcon(icon))
        help_icon = QIcon(theme_icons(name)["help"])
        for button in HelpButton.instances:
            button.setIcon(help_icon)
        self.theme_button.setToolTip(f"Switch to {other} mode")
        self.theme_button.setAccessibleName(f"Switch to {other} mode")

    def _system_theme_changed(self, *_args) -> None:
        if self._theme_override:
            return  # the button's choice wins until it is clicked back
        theme = system_theme(QApplication.instance())
        if theme != self._theme:
            self._set_theme(theme)

    def _toggle_theme(self) -> None:
        theme = "light" if self._theme == "dark" else "dark"
        # Clicking back to the system's appearance resumes following it.
        if theme == system_theme(QApplication.instance()):
            self._theme_override = ""
            self._settings.remove("theme_override")
        else:
            self._theme_override = theme
            self._settings.setValue("theme_override", theme)
        self._settings.sync()
        self._set_theme(theme)

    # ---- input fonts ----------------------------------------------------- #

    def font_paths(self) -> list[str]:
        return [self._item_path(self.font_list.item(i))
                for i in range(self.font_list.count())]

    def add_font_paths(self, paths: list[str]) -> bool:
        """Add fonts to the list. All fonts must share the same OpenType
        features; if any added font differs, none of them are added and an
        error is shown. Returns True when the fonts were added."""
        existing = self.font_paths()
        new: list[str] = []
        for path in paths:
            path = os.path.abspath(path)
            if path not in existing and path not in new:
                new.append(path)
        if not new:
            return True
        problem = self._feature_mismatch(existing, new)
        if problem:
            self._show_error("Fonts not added", problem)
            return False
        for path in new:
            item = QTreeWidgetItem([os.path.basename(path), ""])
            item.setData(0, Qt.ItemDataRole.UserRole, path)  # the full path
            item.setToolTip(0, path)
            self.font_list.addItem(item)
            # Features already frozen into the font start ticked, keeping
            # each glyph as it is until the user changes something.
            for tag in self._frozen(path) - self._selected_tags:
                self._selected_tags.add(tag)
                self._choices["keep"].add(tag)
        self._label_duplicate_names()
        self._follow_first_font_folder()
        self._fonts_changed()
        self._names_timer.start()
        return True

    @staticmethod
    def _item_path(item) -> str:
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _label_duplicate_names(self) -> None:
        """Show file names only; add the folder name when two files share a name."""
        items = [self.font_list.item(i) for i in range(self.font_list.count())]
        names = [os.path.basename(self._item_path(item)) for item in items]
        for item, name in zip(items, names):
            path = self._item_path(item)
            if names.count(name) > 1:
                name += f"  ({os.path.basename(os.path.dirname(path))})"
            item.setText(0, name)

    def _show_error(self, title: str, message: str) -> None:
        QMessageBox.critical(self, title, message)

    def _frozen(self, path: str) -> set[str]:
        """Features already frozen (fully or partly) into a font."""
        if path not in self._frozen_cache:
            try:
                self._frozen_cache[path] = frozen_features(path)
            except Exception:
                self._frozen_cache[path] = set()
        return self._frozen_cache[path]

    def _tags(self, path: str) -> frozenset[str]:
        if path not in self._tag_cache:
            self._tag_cache[path] = feature_tags(path)
        return self._tag_cache[path]

    def _missing_ticked(self, paths: list[str]) -> list[tuple[str, list[str]]]:
        """Fonts that lack any of the ticked features, with what they lack."""
        return [(path, missing) for path in paths
                if (missing := sorted(self._selected_tags - self._tags(path)))]

    @staticmethod
    def _describe_missing(problems: list[tuple[str, list[str]]]) -> str:
        lines = [f"{os.path.basename(path)}: {', '.join(missing)}"
                 for path, missing in problems[:8]]
        if len(problems) > 8:
            lines.append(f"… and {len(problems) - 8} more")
        return "\n".join(lines)

    def _feature_mismatch(self, existing: list[str], new: list[str]) -> str | None:
        """Why the new fonts cannot be added, or None if they can.

        Fonts may have different features; ticked features are frozen only in
        the fonts that have them.
        """
        unreadable = []
        for path in new:
            try:
                self._tags(path)
            except Exception as exc:
                unreadable.append(f"{os.path.basename(path)}: {type(exc).__name__}: {exc}")
        if unreadable:
            return ("None of the fonts were added, because these files could "
                    "not be read as fonts:\n\n" + "\n".join(unreadable[:10]))
        return None

    # Drag and drop from Finder: font files, or folders (searched recursively).

    @staticmethod
    def _dropped_fonts(mime) -> list[str]:
        paths = []
        for url in mime.urls() if mime.hasUrls() else []:
            path = url.toLocalFile()
            if os.path.isdir(path):
                # Every font in the folder and its subfolders, in a stable order.
                candidates = []
                for folder, subfolders, names in os.walk(path):
                    subfolders.sort()
                    candidates.extend(
                        os.path.join(folder, name) for name in sorted(names))
            else:
                candidates = [path]
            paths.extend(
                candidate for candidate in candidates
                if os.path.isfile(candidate)
                and candidate.lower().endswith(FONT_EXTENSIONS))
        return paths

    def _set_drop_highlight(self, active: bool) -> None:
        self.font_list.setProperty("dropTarget", active)
        self.font_list.style().unpolish(self.font_list)
        self.font_list.style().polish(self.font_list)

    def dragEnterEvent(self, event) -> None:
        if self._worker is None and self._dropped_fonts(event.mimeData()):
            event.acceptProposedAction()
            self._set_drop_highlight(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_drop_highlight(False)

    def dropEvent(self, event) -> None:
        self._set_drop_highlight(False)
        paths = self._dropped_fonts(event.mimeData())
        if self._worker is None and paths:
            event.acceptProposedAction()
            self.add_font_paths(paths)
        else:
            event.ignore()

    def _add_fonts(self) -> None:
        # First time: the user's Fonts folder; afterwards the last folder used.
        start = self._last_font_folder or os.path.expanduser("~/Library/Fonts")
        if not os.path.isdir(start):
            start = os.path.expanduser("~")
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select font files", start, FONT_FILTER)
        if paths:
            self._last_font_folder = os.path.dirname(paths[0])
            self.add_font_paths(paths)

    def _remove_fonts(self) -> None:
        for item in self.font_list.selectedItems():
            self.font_list.takeItem(self.font_list.row(item))
        self._label_duplicate_names()
        self._follow_first_font_folder()
        self._names_timer.start()
        self._fonts_changed()

    def _clear_fonts(self) -> None:
        self.font_list.clear()
        self._selected_tags.clear()
        self._choices = {"off": {}, "on": {}, "keep": set()}
        self._follow_first_font_folder()
        self._fonts_changed()

    def _follow_first_font_folder(self) -> None:
        """Until the user picks an output folder, use the first font's folder."""
        current = self.output_edit.text().strip()
        if current not in ("", self._auto_output):
            return  # chosen or typed by the user
        paths = self.font_paths()
        folder = os.path.dirname(paths[0]) if paths else ""
        self._auto_output = folder
        self.output_edit.setText(folder)

    def _choose_output(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select the output folder", self.output_edit.text())
        if folder:
            self.output_edit.setText(folder)

    def _active_path(self) -> str | None:
        """The font to show: the selected one, otherwise the first in the list."""
        selected = self.font_list.selectedItems()
        current = self.font_list.currentItem()
        if current is not None and current in selected:
            return self._item_path(current)
        if selected:
            return self._item_path(selected[0])
        return self._item_path(self.font_list.item(0)) if self.font_list.count() else None

    def _fonts_changed(self) -> None:
        if self.font_list.count() and not self.font_list.selectedItems():
            self.font_list.setCurrentRow(0)  # re-enters with a selection
            return
        first = self._active_path()
        if first == self._active_font:
            if tuple(self.font_paths()) != self._listed_fonts:
                self._listed_fonts = tuple(self.font_paths())
                self._rebuild_features()  # "not in all fonts" may have changed
            return
        self._listed_fonts = tuple(self.font_paths())
        self._active_font = first
        self._font_info = empty_font_info()
        if first:
            try:
                self._font_info = read_font_info(first)
            except Exception as exc:
                QMessageBox.warning(
                    self, "Cannot read font",
                    f"{first}\n\n{type(exc).__name__}: {exc}")
        self._rebuild_features()
        self._rebuild_tags()
        # Keep text the user typed; only replace text the app filled in itself.
        if (self.preview_edit.toPlainText() in ("", self._auto_preview_text)
                and not self._hide_unchanged):
            self._show_sample()
        self._update_preview()

    # ---- features, scripts, languages ------------------------------------ #

    def selected_features(self) -> list[str]:
        return sorted(self._selected_tags)

    def _update_file_count(self, *_args) -> None:
        self.files_title.setText(f"Font files to process ({self.font_list.count()})")

    def _rename_settings(self) -> tuple[str, str] | None:
        """(custom suffix, replace names) as used for renaming, or None if
        replace names is not valid."""
        replacenames = self.replacenames_edit.text().strip()
        if replacenames and not valid_replacenames(replacenames):
            return None
        return self.suffix_edit.text().strip(), replacenames

    def _planned_outputs(self, overwrite: bool) -> list[tuple[str, str | None]]:
        """(input path, output file name) for every font, as Generate would
        name them with or without overwriting each font's own file."""
        settings = self._rename_settings()
        folder = self.output_edit.text().strip()
        folder = os.path.abspath(os.path.expanduser(folder)) if folder else ""
        paths = self.font_paths()
        taken = {os.path.realpath(path) for path in paths}
        plan = []
        for path in paths:
            try:
                filename = predict_names(path, *settings)["filename"] if settings else None
            except Exception:
                filename = None
            if filename and folder:
                filename = os.path.basename(
                    output_target(folder, filename, path, taken, overwrite))
            plan.append((path, filename))
        return plan

    def _ask_overwrite(self, clashes: list[str]) -> str | None:
        """Ask whether to overwrite input fonts or add a suffix, then confirm.
        Returns "overwrite", "suffix" or None (cancelled)."""
        listed = "\n".join(os.path.basename(path) for path in clashes[:10])
        if len(clashes) > 10:
            listed += f"\n… and {len(clashes) - 10} more"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Output files would replace the input files")
        box.setText(f"{len(clashes)} output file(s) have the same name and "
                    "folder as the font they are made from:")
        box.setInformativeText(listed + "\n\nOverwrite the original files, or "
                               "save the new ones with \"-featfreeze\" added?")
        overwrite = box.addButton("Overwrite originals",
                                  QMessageBox.ButtonRole.DestructiveRole)
        suffix = box.addButton("Add suffix", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(suffix)
        box.exec()
        if box.clickedButton() is overwrite:
            choice = "overwrite"
            question = (f"Overwrite {len(clashes)} original font file(s)? The "
                        "originals will be replaced and cannot be recovered.")
        elif box.clickedButton() is suffix:
            choice = "suffix"
            question = (f"Save {len(clashes)} font file(s) with \"-featfreeze\" "
                        "added to the name, keeping the originals?")
        else:
            return None
        answer = QMessageBox.question(
            self, "Confirm", question,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel if choice == "overwrite"
            else QMessageBox.StandardButton.Ok)
        return choice if answer == QMessageBox.StandardButton.Ok else None

    def _update_output_names(self) -> None:
        """Fill in each font's output file name and the selected font's
        PostScript name, exactly as Generate will name them."""
        settings = self._rename_settings()
        folder = self.output_edit.text().strip()
        folder = os.path.abspath(os.path.expanduser(folder)) if folder else ""
        paths = self.font_paths()
        planned = dict(self._planned_outputs(overwrite=True))
        names_of = {}
        for row, path in enumerate(paths):
            item = self.font_list.item(row)
            if settings is None:
                item.setText(1, "replace names needs search/replace pairs")
                continue
            try:
                names = predict_names(path, *settings)
                # Shown as proposed; a clash with the font's own file is
                # resolved only when Generate is clicked.
                filename = planned.get(path) or names["filename"]
            except Exception as exc:
                names, filename = {}, f"cannot tell: {type(exc).__name__}"
            names_of[path] = names
            item.setText(1, filename)
            postscript = names.get("postscript", "")
            item.setToolTip(1, f"PostScript name: {postscript}" if postscript else "")
        selected = names_of.get(self._active_font, {})
        original = {}
        if self._active_font:
            try:
                original = predict_names(self._active_font, "", "")
            except Exception:
                pass
        for key in ("family", "style", "full", "postscript"):
            self._name_labels[(key, "input")].setText(original.get(key) or "–")
            self._name_labels[(key, "output")].setText(selected.get(key) or "–")
        QTimer.singleShot(0, self._fit_names)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt naming)
        if watched is getattr(self, "_names_panel", None) and \
                event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._fit_names)
        return super().eventFilter(watched, event)

    def _fit_names(self) -> None:
        """Keep the names panel tall enough for its wrapped lines, and the
        options below it at full height."""
        panel = self._names_panel
        height = panel.layout().totalHeightForWidth(panel.width())
        if height > 0 and height != panel.minimumHeight():
            panel.setMinimumHeight(height)
        options = self._options_panel
        needed = options.sizeHint().height()
        if needed != options.minimumHeight():
            options.setMinimumHeight(needed)

    def _feature_toggled(self, checked: bool) -> None:
        tag = self.sender().property("tag")
        (self._selected_tags.add if checked else self._selected_tags.discard)(tag)
        # Ticked by hand, the whole feature is frozen (except unticked glyphs).
        self._choices["keep"].discard(tag)
        self._refresh_subs_buttons()
        self._schedule_preview()

    def _update_features_extra(self) -> None:
        shown = getattr(self, "_shown_tags", set())
        extra = sorted(self._selected_tags - shown)
        self.features_extra.setText(
            "Also selected, but not in this font: " + ", ".join(extra))
        self.features_extra.setVisible(bool(extra))

    def _rebuild_features(self) -> None:
        checked = self._selected_tags
        self._feature_boxes = []
        self._subs_buttons: dict[str, tuple[QPushButton, dict]] = {}
        container = QWidget()
        grid = QGridLayout(container)
        grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        features = list(self._font_info["features"])
        # Features only in other input fonts can be ticked too (pairs=None).
        here = {tag for tag, _, _ in features}
        elsewhere = set()
        for path in self.font_paths():
            try:
                elsewhere |= self._tags(path)
            except Exception:
                pass
        features += [(tag, friendly_feature_name(tag), None)
                     for tag in elsewhere - here]
        features.sort(key=lambda f: f[0])
        self._shown_tags = {tag for tag, _, _ in features}
        if not features:
            text = ("This font has no GSUB features." if self._active_font
                    else "Add a font to list its OpenType features.")
            grid.addWidget(_label(text, "hint"), 0, 0)
        rows = -(-len(features) // FEATURE_COLUMNS)
        for index, (tag, name, pairs) in enumerate(features):
            count = ""
            if pairs is None:
                having = [os.path.basename(p) for p in self.font_paths()
                          if tag in self._tags(p)]
                count = "not in this font"
                tip = ("Only in: " + ", ".join(having[:10])
                       + (f" and {len(having) - 10} more" if len(having) > 10 else "")
                       + "\n\nTicked, it is frozen in those fonts; select one "
                       "of them to preview it or choose its substitutions.")
            elif pairs:
                tip = ""
                frozen = self._font_info.get("frozen", {}).get(tag)
                if frozen:
                    count = ("already frozen" if frozen["full"]
                             else "already partly frozen")
                    tip = (("This feature was already frozen into this font."
                            if frozen["full"] else
                            "Part of this feature was already frozen into "
                            "this font.")
                           + " Ticked, the frozen glyphs stay frozen; untick "
                           "the feature, or glyphs in its substitution list, "
                           "to give characters their original glyphs back.")
            else:
                count = "not freezable"
                tip = ("This feature has no single or alternate substitutions "
                       "(it uses ligatures or contextual rules), so freezing "
                       "it will not change the font.")
            lacking = [os.path.basename(path) for path in self.font_paths()
                       if tag not in self._tags(path)]
            if lacking and pairs is not None:
                count += ("  ·  " if count else "") + "not in all fonts"
                tip += ("\n\n" if tip else "") + "Not in: " + ", ".join(lacking[:10]) + (
                    f" and {len(lacking) - 10} more" if len(lacking) > 10 else "")
            box = ElidingCheckBox("  ·  ".join(part for part in (tag, name, count) if part))
            box.setObjectName("feature")
            box.setProperty("tag", tag)
            box.setChecked(tag in checked)
            box.setToolTip(tip)
            # Greyed out when it cannot be frozen; features of other fonts
            # stay tickable.
            box.setEnabled(pairs is None or bool(pairs))
            box.toggled.connect(self._feature_toggled)
            if tag in self._choices["keep"] and tag in checked:
                box.setToolTip(tip + ("\n\n" if tip else "") + (
                    "Ticked because it was already frozen in one of the "
                    "fonts: each font keeps its frozen glyphs as they are "
                    "unless you change them."))
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(box, 1)  # shortened to fit; buttons stay right
            if pairs:
                row_layout.addWidget(self._substitutions_button(tag, name, pairs))
            grid.addWidget(row, index % rows, index // rows)
            self._feature_boxes.append(box)
        self.features_area.setWidget(container)
        self.features_title.setText("OpenType features")
        self._update_features_extra()

    def _encoded(self) -> dict:
        """{glyph: code points} of the font shown."""
        info = self._font_info
        if "encoded" not in info:
            info["encoded"] = encoded_glyphs(info["cmap"])
        return info["encoded"]

    def _pair_states(self, tag: str, pairs: dict) -> dict:
        """{source glyph: True/False/None} for the font shown: whether the
        substitution will be frozen if the feature is ticked, or None when
        zapped glyph names hide whether it is frozen (left as it is)."""
        encoded, zapped = self._encoded(), self._font_info.get("zapped", set())
        states = {}
        for source, target in pairs.items():
            if undeterminable(source, target, encoded, zapped):
                states[source] = None
            else:
                states[source] = pair_wanted(
                    tag, source, in_effect(source, target, encoded),
                    {tag}, self._choices)
        return states

    def _substitutions_button(self, tag: str, name: str, pairs: dict) -> QPushButton:
        button = QPushButton()
        button.setObjectName("small")
        button.setToolTip("Show this feature's glyph substitutions and choose "
                          "which ones to freeze")
        button.setAccessibleName(f"{tag} substitutions")
        button.clicked.connect(
            lambda: self._edit_substitutions(tag, name, pairs))
        self._subs_buttons[tag] = (button, pairs)
        self._label_subs_button(tag)
        return button

    def _label_subs_button(self, tag: str) -> None:
        button, pairs = self._subs_buttons[tag]
        total = len(pairs)
        on = sum(state is not False for state in self._pair_states(tag, pairs).values())
        unit = "glyph" if total == 1 else "glyphs"
        button.setText(f"{on} of {total} {unit}" if on < total else f"{total} {unit}")
        if button.property("partial") != (on < total):
            button.setProperty("partial", on < total)
            button.style().unpolish(button)
            button.style().polish(button)

    def _refresh_subs_buttons(self) -> None:
        for tag in getattr(self, "_subs_buttons", {}):
            self._label_subs_button(tag)

    def _edit_substitutions(self, tag: str, name: str, pairs: dict) -> None:
        if not self._active_font:
            return
        saved = {key: {t: set(g) for t, g in self._choices[key].items()}
                 for key in ("off", "on")}
        current = self._pair_states(tag, pairs)
        # What the user's own choices would give if they had made none.
        plain = dict(self._choices, off={}, on={})
        encoded = self._encoded()
        default_of = {source: pair_wanted(tag, source, in_effect(source, target, encoded),
                                          {tag}, plain)
                      for source, target in pairs.items()}

        def apply(states: dict) -> None:
            off = set(saved["off"].get(tag, set())) - set(states)
            on = set(saved["on"].get(tag, set())) - set(states)
            for source, checked in states.items():
                if checked != default_of[source]:
                    (on if checked else off).add(source)
            for key, glyphs in (("off", off), ("on", on)):
                if glyphs:
                    self._choices[key][tag] = glyphs
                else:
                    self._choices[key].pop(tag, None)

        dialog = SubstitutionDialog(
            self, self._active_font, tag, name, pairs, self._font_info["cmap"],
            current, self._font_info.get("zapped", set()),
            self._font_info.get("frozen", {}).get(tag, {}).get("glyphs", set()))
        # Every tick or untick updates the preview straight away; the feature
        # is previewed even if it is not ticked in the feature list.
        dialog.changed.connect(lambda states: (apply(states),
                                               self._schedule_preview()))
        self._previewing_tag = tag
        self._place_beside_preview(dialog)
        self._update_preview()
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._previewing_tag = None
        if accepted:
            apply(dialog.states())
        else:  # Cancel restores
            for key in ("off", "on"):
                self._choices[key] = saved[key]
        dialog.deleteLater()
        self._rebuild_features()
        self._update_preview()

    def _place_beside_preview(self, dialog: QDialog) -> None:
        """Put the dialog left of the preview so the preview stays visible."""
        frame = self.frameGeometry()
        dialog.resize(dialog.width(), min(dialog.height(), frame.height() - 80))
        preview_left = self.preview_edit.mapToGlobal(QPoint(0, 0)).x()
        x = max(frame.left() + 16, preview_left - dialog.width() - 24)
        dialog.move(x, frame.top() + 60)

    def _reset_features(self) -> None:
        """Back to the input fonts' own state: features already frozen into
        any of them ticked (each glyph kept as it is), nothing else."""
        frozen = set()
        for path in self.font_paths():
            frozen |= self._frozen(path)
        self._selected_tags = set(frozen)
        self._choices = {"off": {}, "on": {}, "keep": set(frozen)}
        self._rebuild_features()
        self._schedule_preview()

    def _set_all_features(self, state: bool) -> None:
        for box in self._feature_boxes:
            box.blockSignals(True)
            box.setChecked(state)
            box.blockSignals(False)
        if state:
            self._selected_tags.update(
                box.property("tag") for box in self._feature_boxes)
        else:
            self._selected_tags.clear()  # including features of other fonts
        self._choices["keep"].clear()
        self._refresh_subs_buttons()
        self._update_features_extra()
        self._schedule_preview()

    def _rebuild_tags(self) -> None:
        script = self.script_combo.currentText()
        self.script_combo.blockSignals(True)
        self.script_combo.clear()
        self.script_combo.addItem("")
        self.script_combo.addItems(
            sorted(tag.strip() for tag in self._font_info["scripts"]))
        self.script_combo.setCurrentText(script)
        self.script_combo.blockSignals(False)
        self._rebuild_languages()

    def _rebuild_languages(self) -> None:
        scripts = self._font_info["scripts"]
        script = pad_tag(self.script_combo.currentText())
        if script in scripts:
            langs = set(scripts[script])
        else:
            langs = {lang for tags in scripts.values() for lang in tags}
        lang = self.lang_combo.currentText()
        self.lang_combo.blockSignals(True)
        self.lang_combo.clear()
        self.lang_combo.addItem("")
        self.lang_combo.addItems(sorted(tag.strip() for tag in langs))
        self.lang_combo.setCurrentText(lang)
        self.lang_combo.blockSignals(False)

    def _script_changed(self) -> None:
        self._rebuild_languages()
        self._schedule_preview()

    # ---- preview --------------------------------------------------------- #

    def _schedule_preview(self, *_args) -> None:
        self._preview_timer.start()

    def _update_preview(self) -> None:
        old_id = self._preview_font_id
        self._preview_font_id = -1
        self._preview_family = ""
        self._changed = set()
        status = ""
        if self._active_font:
            features = self.selected_features()
            note = ""
            if self._previewing_tag and self._previewing_tag not in features:
                features = sorted(features + [self._previewing_tag])
                note = f" ({self._previewing_tag} shown while its substitutions are open)"
            self._preview_counter += 1
            family = f"FeatFreezePreview{self._preview_counter}"
            try:
                data, self._changed = build_preview_font(
                    self._active_font, ",".join(features),
                    pad_tag(self.script_combo.currentText()) or None,
                    pad_tag(self.lang_combo.currentText()) or None,
                    family, self._choices)
                font_id = QFontDatabase.addApplicationFontFromData(
                    QByteArray(data))
                families = QFontDatabase.applicationFontFamilies(font_id)
                if font_id < 0 or not families:
                    raise RuntimeError("the font could not be loaded for display")
                self._preview_font_id = font_id
                self._preview_family = families[0]
                shown = ", ".join(features) if features else "no features"
                status = (f"{os.path.basename(self._active_font)} with "
                          f"{shown}{note}: ", "")
            except Exception as exc:
                status = (f"Preview unavailable: {type(exc).__name__}: {exc}", None)
        self._status_text = status if isinstance(status, tuple) else ("", None)
        self._apply_preview_font()
        self._refresh_changed_highlight()
        self._names_timer.start()  # features or the selected font changed
        if self._hide_unchanged:
            self._show_changed_characters()  # keep the list current
        if old_id >= 0:
            QFontDatabase.removeApplicationFont(old_id)

    def _apply_preview_font(self, *_args) -> None:
        size = self.size_spin.value()
        if self._preview_family:
            styles = QFontDatabase.styles(self._preview_family)
            if styles:
                font = QFontDatabase.font(self._preview_family, styles[0], size)
            else:
                font = QFont(self._preview_family)
            font.setPointSize(size)
            font.setStyleStrategy(QFont.StyleStrategy.NoFontMerging)
        else:
            font = QFont(self.font())
            font.setPointSize(size)
        self.preview_edit.setFont(font)

    def _refresh_changed_highlight(self) -> None:
        """Colour the preview characters the selected features change, and
        count them in the status line."""
        text = self.preview_edit.toPlainText()
        color = QColor(THEMES[self._theme]["changed"])
        # Collect runs of adjacent changed characters; the preview box paints
        # every glyph in them in the highlight colour.
        runs: list[list[int]] = []  # [start, end) in UTF-16 code units
        present: set[int] = set()
        position = 0  # QTextDocument positions count UTF-16 code units
        for char in text:
            size = 2 if ord(char) > 0xFFFF else 1
            if ord(char) in self._changed:
                present.add(ord(char))
                if runs and runs[-1][1] == position:
                    runs[-1][1] += size
                else:
                    runs.append([position, position + size])
            position += size
        self.preview_edit.set_highlight(
            [tuple(run) for run in runs], color,
            QColor(THEMES[self._theme]["field"]))

        prefix, note = self._status_text
        if note is None:  # an error message
            self.preview_status.setText(prefix)
        elif prefix:
            self.preview_status.setText(
                f"{prefix}{len(present)} character(s) changed (shown in blue) "
                "in the sample text above.")
        else:
            self.preview_status.setText("")

    def _fill_preview(self, text: str) -> None:
        self._auto_preview_text = text
        self._filling_preview = True
        try:
            self.preview_edit.setPlainText(text)
        finally:
            self._filling_preview = False

    def _sample_text(self) -> str:
        info = self._font_info
        if self._sample == "western":
            return western_characters(info["cmap"], info["ligatures"])
        if self._sample == "all":
            return main_characters(info["cmap"], info["ligatures"])
        if self._sample == "prose":
            return PROSE_SAMPLE
        if self._sample == "custom":
            return self._custom_text
        return CODE_SAMPLES[self._sample.removeprefix("code:")].rstrip("\n")

    def _show_sample(self) -> None:
        # Code keeps its lines (scroll sideways); other samples wrap.
        self.preview_edit.setLineWrapMode(
            QTextEdit.LineWrapMode.NoWrap if self._sample.startswith("code:")
            else QTextEdit.LineWrapMode.WidgetWidth)
        if self._sample == "custom":
            placeholder = ("Type your own sample text here. It is kept, even "
                           "after quitting, until you change it.")
        else:
            placeholder = ("Add a font to see a preview. You can also type "
                           "your own text here.")
        self.preview_edit.setPlaceholderText(placeholder)
        if self._active_font or self._sample in ("custom", "prose") or \
                self._sample.startswith("code:"):
            self._fill_preview(self._sample_text())
        else:
            self._fill_preview("")

    def _label_sample_button(self) -> None:
        self.sample_button.setText(
            "Sample: " + SAMPLE_NAMES[self._sample].removeprefix("Code: "))

    def _choose_sample(self, key: str) -> None:
        self._sample = key
        self._settings.setValue("sample", key)
        self._label_sample_button()
        if self._hide_unchanged:  # show the chosen sample, not the changes
            self._toggle_unchanged()
        else:
            self._show_sample()
        if key == "custom":
            self.preview_edit.setFocus()

    def _keep_custom_text(self) -> None:
        """Remember what the user types while the custom sample is shown."""
        if (self._sample == "custom" and not self._hide_unchanged
                and not self._filling_preview):
            self._custom_text = self.preview_edit.toPlainText()
            self._auto_preview_text = self._custom_text
            self._settings.setValue("custom_text", self._custom_text)

    def _show_changed_characters(self) -> None:
        self.preview_edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.preview_edit.setPlaceholderText(
            "No characters change with the current selection.")
        text = main_characters(self._changed)
        if text != self.preview_edit.toPlainText():
            self._fill_preview(text)

    def _toggle_unchanged(self) -> None:
        self._hide_unchanged = not self._hide_unchanged
        self.unchanged_button.setText("Show unchanged characters"
                                      if self._hide_unchanged
                                      else "Hide unchanged characters")
        if self._hide_unchanged:
            self._show_changed_characters()
        else:
            self._show_sample()

    # ---- processing ------------------------------------------------------ #

    def _log(self, text: str) -> None:
        self.log_edit.appendPlainText(text)
        self.log_edit.moveCursor(QTextCursor.MoveOperation.End)

    def _collect_settings(self) -> dict | None:
        """Validate the form. Returns the settings, or None after showing an error."""
        def fail(message: str) -> None:
            QMessageBox.warning(self, "Cannot generate fonts", message)

        if not self.font_paths():
            return fail("Add at least one input font file.")
        output = self.output_edit.text().strip()
        if not output:
            return fail("Choose an output folder.")
        output = os.path.abspath(os.path.expanduser(output))
        try:
            os.makedirs(output, exist_ok=True)
        except OSError as exc:
            return fail(f"Cannot create the output folder:\n{exc}")
        replacenames = self.replacenames_edit.text().strip()
        if replacenames and not valid_replacenames(replacenames):
            return fail(
                "Replace names must be search/replace pairs or words to remove, "
                "separated by commas, for example:\n\nInter/Outer,Display/Headline,OT 2")
        suffix = self.suffix_edit.text().strip()
        return {
            "output": output,
            "features": ",".join(self.selected_features()),
            "script": pad_tag(self.script_combo.currentText()) or None,
            "lang": pad_tag(self.lang_combo.currentText()) or None,
            "zapnames": self.zapnames_check.isChecked(),
            # The family name gets the custom suffix only, never feature tags.
            "suffix": bool(suffix),   # a custom suffix is used whenever one is typed
            "usesuffix": suffix,
            "replacenames": replacenames,
            "info": self.info_check.isChecked(),
            "report": self.report_check.isChecked(),
            "names": self.names_check.isChecked(),
            "verbose": self.verbose_check.isChecked(),
            "choices": {"off": {t: set(g) for t, g in self._choices["off"].items()},
                        "on": {t: set(g) for t, g in self._choices["on"].items()},
                        "keep": set(self._choices["keep"])},
        }

    def _generate(self) -> None:
        if self._worker is not None:
            return
        settings = self._collect_settings()
        if settings is None:
            return
        paths = self.font_paths()
        folder = settings.pop("output")
        missing = self._missing_ticked(paths)  # reported, not an error

        clashes = [path for path, name in self._planned_outputs(overwrite=True)
                   if name and os.path.realpath(os.path.join(folder, name))
                   == os.path.realpath(path)]
        overwrite = False
        if clashes:
            choice = self._ask_overwrite(clashes)
            if choice is None:
                return
            overwrite = choice == "overwrite"
            if not overwrite:  # show the names the files will really get
                for row, (_, name) in enumerate(self._planned_outputs(overwrite=False)):
                    if name:
                        self.font_list.item(row).setText(1, name)

        self.log_edit.clear()
        if missing:
            self._log("Ticked features missing from some fonts (those fonts "
                      "are processed without them):")
            for line in self._describe_missing(missing).split("\n"):
                self._log("  " + line)
        if overwrite:
            self._log("Overwriting the original font files: "
                      + ", ".join(os.path.basename(path) for path in clashes))
        choices = settings["choices"]
        frozen = {tag for path in paths for tag in self._frozen(path)}
        if not settings["features"] and not frozen:
            self._log("Note: no features are selected, so no glyphs will be remapped.")
        for tag in sorted(frozen - self._selected_tags):
            self._log(f"Reversing {tag}, which was already frozen, in the fonts "
                      "that have it frozen")
        for tag in sorted(self._selected_tags):
            if tag in choices["keep"]:
                self._log(f"Keeping {tag} as already frozen in each font")
            if choices["on"].get(tag) and tag in choices["keep"]:
                self._log(f"Also freezing in {tag}: "
                          + ", ".join(sorted(choices["on"][tag])))
            if choices["off"].get(tag):
                self._log(f"Not frozen in {tag} (reversed where already frozen): "
                          + ", ".join(sorted(choices["off"][tag])))
        if (settings["info"] and not settings["suffix"]
                and not settings["replacenames"]):
            self._log("Note: the version string is only updated when a suffix "
                      "or replace names is also used.")

        for panel in self._control_panels:
            panel.setEnabled(False)
        self.generate_button.setEnabled(False)
        self.progress_bar.setRange(0, len(paths))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        self._worker = FreezeWorker(paths, folder, settings, overwrite, self)
        self._worker.message.connect(self._log)
        self._worker.progress.connect(self.progress_bar.setValue)
        self._worker.completed.connect(self._generation_done)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _generation_done(self, saved: int, failed: int) -> None:
        self._worker = None
        for panel in self._control_panels:
            panel.setEnabled(True)
        self.generate_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        summary = f"Done: {saved} font file(s) saved"
        if failed:
            summary += f", {failed} failed"
        self._log(summary + ".")

    def closeEvent(self, event) -> None:
        if self._worker is not None:
            self._worker.wait()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("OpenType Feature Freezer")
    window = MainWindow()
    # Font files given on the command line are added to the list.
    paths = [arg for arg in sys.argv[1:] if os.path.isfile(arg)]
    if paths:
        window.add_font_paths(paths)
    window.show()
    exit_code = app.exec()
    shut_down(window)
    return exit_code


def shut_down(window: MainWindow) -> None:
    """Tear the window down ourselves, then exit without Python's clean-up.

    When the interpreter exits, PySide deletes every widget Python still
    holds a reference to. Widgets added to a layout before that layout was
    attached to a window can be deleted twice that way, which crashes the app
    on quit (a segfault in ~QPushButton). Deleting the window here, while
    Qt's parent/child bookkeeping is intact, and skipping the interpreter's
    teardown avoids that.
    """
    import shiboken6

    window._settings.sync()       # flush any saved settings before exiting
    HelpButton.instances.clear()
    if shiboken6.isValid(window):
        shiboken6.delete(window)
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()


if __name__ == "__main__":
    code = main()
    os._exit(code)  # skip interpreter teardown, see shut_down()
