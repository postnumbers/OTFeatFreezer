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
    from fontTools import unicodedata as ft_unicodedata
    from fontTools.pens.qtPen import QtPen
    from fontTools.ttLib import TTFont
    from opentype_feature_freezer import RemapByOTL
    from PySide6.QtCore import QByteArray, QSettings, Qt, QThread, QTimer, Signal
    from PySide6.QtCore import QPoint, QSize
    from PySide6.QtGui import (
        QPainter, QPainterPath, QPixmap, QTransform,
        QColor, QFont, QFontDatabase, QIcon, QPalette, QTextCursor)
    from PySide6.QtWidgets import (
        QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
        QDialogButtonBox, QFileDialog, QFrame,
        QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
        QListWidgetItem, QMainWindow, QSizePolicy, QTreeWidget, QTreeWidgetItem,
        QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
        QSpinBox, QSplitter, QTextEdit, QToolButton, QVBoxLayout, QWidget,
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
                    "them, format is 'search1/replace1,search2/replace2,...'",
    "info": "update font version string",
    "report": "report languages, scripts and features in font",
    "names": "output names of remapped glyphs during processing",
    "verbose": "print additional information during processing",
}
# Labels shown on the controls (the app's option names are kept in HELP keys).
LABELS = {
    "zapnames": "zap glyph names",
    "suffix": "add suffix to font family name",
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
    "suffix": "Add the custom suffix to the font family name, e.g. 'Inter' "
              "becomes 'Inter SC'. OpenType feature tags are never added to "
              "the family name. Output files and PostScript names follow the "
              "new family name.",
    "usesuffix": "The suffix added to the font family name when 'add suffix "
                 "to font family name' is ticked, e.g. 'SC'.",
    "replacenames": "Change the font family name by replacing text in it, "
                    "e.g. Inter/Outer renames the Inter family to Outer. Only "
                    "the family name is searched (not the style name), and the "
                    "search is case-sensitive. The full name, PostScript name "
                    "and output file name follow the new family name. Format: "
                    "search1/replace1,search2/replace2,...",
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
              "in from Finder. Each font must contain the features you tick. "
              "Select a font to show its features, preview and PostScript name.",
    "output": "Folder for the output .otf or .ttf font files. Each file is "
              "named after the font's new name; input fonts are never "
              "overwritten.",
    "features": "Tick the features to freeze. The button beside each feature "
                "shows how many glyph substitutions it can freeze; click it to "
                "see them and switch individual substitutions on or off for "
                "the output fonts. Features marked 'not in all fonts' are "
                "missing from some input fonts.",
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
        color = THEMES[name][{"check": "on_accent", "help": "muted"}.get(shape, "fg")]
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
    return {"features": [], "scripts": {}, "cmap": {},
            "ligatures": {"standard": [], "coding": []}}


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


def replace_in_filename(stem: str, replacenames: str) -> str:
    """Apply the replace names pairs to a file name (without extension).

    Each pair is applied as typed and with its spaces removed, because file
    names follow PostScript names, which have no spaces. Case-sensitive.
    """
    for pair in replacenames.split(",") if replacenames else ():
        if pair.count("/") != 1:
            continue
        search, replace = pair.split("/")
        if search:
            stem = stem.replace(search, replace)
            compact = search.replace(" ", "")
            if compact and compact != search:
                stem = stem.replace(compact, replace.replace(" ", ""))
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", stem).strip()


def output_filename(font: TTFont, inpath: str, renamed: bool,
                    replacenames: str = "") -> str:
    """Name the output after the font's new PostScript name, e.g.
    'Inter-Bold.otf' -> 'InterFrozen-Bold.otf', with the replace names pairs
    also applied to the file name. Unrenamed fonts keep their name."""
    source = Path(inpath)
    stem = source.stem
    if renamed:
        postscript_name = font["name"].getDebugName(6) or ""
        postscript_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", postscript_name).strip()
        if postscript_name:
            stem = postscript_name
    stem = replace_in_filename(stem, replacenames) or source.stem
    return f"{stem}{source.suffix}"


def remap_glyphs(remapper: RemapByOTL, disabled: dict | None = None) -> None:
    """Remap the font's cmap like RemapByOTL.remapByOTL(), but skip excluded
    substitutions.

    `disabled` maps a feature tag to the source glyph names whose substitution
    should not be frozen. A lookup shared by several ticked features skips a
    glyph only when every one of those features excludes it.
    """
    disabled = disabled or {}
    remapper.initSubs()
    remapper.filterFeatureIndex()
    remapper.filterLookupList()
    font = remapper.ttx
    if "GSUB" not in font:
        return  # nothing to freeze; the cmap stays as it is
    gsub = font["GSUB"].table
    users: dict[int, set[str]] = {}  # lookup index -> ticked features using it
    for index in remapper.FeatureIndex:
        record = gsub.FeatureList.FeatureRecord[index]
        if record.FeatureTag in remapper.filterByFeatures:
            for lookup_index in record.Feature.LookupListIndex:
                users.setdefault(lookup_index, set()).add(record.FeatureTag)

    subs = remapper.subs1
    for lookup_index in remapper.LookupList:
        tags = users.get(lookup_index, set())
        blocked = (set.intersection(*(set(disabled.get(tag, ())) for tag in tags))
                   if tags else set())
        for subtable in gsub.LookupList.Lookup[lookup_index].SubTable:
            if subtable.LookupType == 7:
                subtable = subtable.ExtSubTable
            if subtable.LookupType == 1:
                pairs = list(subtable.mapping.items())
            elif subtable.LookupType == 3:  # alternates: take the first, as the library does
                pairs = [(glyph, alternates[0]) for glyph, alternates
                         in subtable.alternates.items() if alternates]
            else:
                continue
            for sub_in, sub_out in pairs:
                if sub_in in blocked:
                    continue
                for i, glyph in enumerate(subs):
                    if glyph == sub_in:
                        subs[i] = sub_out

    with_unicode = {glyph for table in font["cmap"].tables
                    for glyph in table.cmap.values()}
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


def freeze_font(options: SimpleNamespace, folder: str, taken: set[str],
                disabled: dict | None = None, overwrite: bool = False) -> str | None:
    """Freeze one font into `folder`. Returns the saved path, or None on failure."""
    remapper = RemapByOTL(options)
    remapper.openFont()
    if not remapper.success:
        return None
    try:
        remap_glyphs(remapper, disabled)
        names = remapper.ttx["name"]
        old_family = names.getDebugName(16) or names.getDebugName(1) or ""
        old_postscript = names.getDebugName(6) or ""
        remapper.renameFont()
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
        filename = output_filename(remapper.ttx, options.inpath, renamed,
                                   options.replacenames)
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

    Mirrors RemapByOTL.renameFont() on the name table. Returns a dict with
    'filename', 'family', 'full' (family plus style) and 'postscript'.
    """
    font = TTFont(path, lazy=True)
    try:
        name = font["name"]

        def get(name_id: int) -> str:
            record = name.getName(name_id, 3, 1)
            return record.toStr() if record is not None else (
                name.getDebugName(name_id) or "")

        family = old_family = get(16) or get(1)
        full, postscript = get(4), get(6)
        if not suffix and not replacenames:
            return {"filename": os.path.basename(path), "family": family,
                    "full": full, "postscript": postscript}
        if name.getName(16, 3, 1) is None and name.getName(1, 3, 1) is None:
            raise ValueError("no Windows family name to rename")
        for search, replace in (pair.split("/") for pair in replacenames.split(",")
                                if replacenames):
            family = family.replace(search, replace)
        new_family = f"{family} {suffix}" if suffix else family
        full = full.replace(old_family, new_family)
        postscript = postscript.replace(old_family.replace(" ", ""),
                                        new_family.replace(" ", ""))
        stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", postscript).strip()
        stem = replace_in_filename(stem or Path(path).stem, replacenames)
        return {"filename": f"{stem or Path(path).stem}{Path(path).suffix}",
                "family": new_family, "full": full, "postscript": postscript}
    finally:
        font.close()


def build_preview_font(path: str, features: str, script, lang, family: str,
                       disabled: dict | None = None):
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
        remap_glyphs(remapper, disabled)
        mapping = remapper.substitution_mapping
        changed = {cp for cp, glyph in before.items()
                   if mapping.get(glyph, glyph) != glyph}

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
                                              settings["disabled"], self._overwrite)
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


class SubstitutionDialog(QDialog):
    """Lists one feature's glyph substitutions with a checkbox each."""

    GLYPH_SIZE = 34
    changed = Signal(object)  # the set of excluded source glyphs, on every change

    def __init__(self, parent, font_path: str, tag: str, name: str,
                 pairs: dict, cmap: dict, disabled: set[str]):
        super().__init__(parent)
        self.setWindowTitle(f"{tag} substitutions")
        self.resize(560, 640)
        layout = QVBoxLayout(self)
        layout.addWidget(_label(f"{tag}  ·  {name}" if name else tag, "title"))
        layout.addWidget(_label(
            f"Substitutions in {os.path.basename(font_path)}. Untick one to "
            "leave that glyph unchanged in all output fonts.", "hint"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by glyph name or character")
        self.filter_edit.textChanged.connect(self._filter)
        layout.addWidget(self.filter_edit)

        self.list = QListWidget()
        size = self.GLYPH_SIZE
        self.list.setIconSize(QSize(size * 3, size))
        self.list.setUniformItemSizes(True)
        layout.addWidget(self.list, 1)
        char_of = {}
        for codepoint in sorted(cmap, reverse=True):
            char_of[cmap[codepoint]] = codepoint
        color = self.palette().color(QPalette.ColorRole.Text)
        painter = GlyphPainter(font_path)
        try:
            for source, target in pairs.items():
                text = f"{source}  →  {target}"
                if source in char_of:
                    text += f"     {chr(char_of[source])}  U+{char_of[source]:04X}"
                item = QListWidgetItem(QIcon(self._pair_picture(
                    painter, source, target, color)), text)
                item.setData(Qt.ItemDataRole.UserRole, source)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked if source in disabled
                                   else Qt.CheckState.Checked)
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
        self.list.itemChanged.connect(lambda *_: self.changed.emit(self.disabled()))
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

    def _set_visible(self, enabled: bool) -> None:
        state = Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
        for item in self._items():
            if not item.isHidden():
                item.setCheckState(state)

    def _update_count(self, *_args) -> None:
        on = sum(item.checkState() == Qt.CheckState.Checked for item in self._items())
        self.count_label.setText(f"{on} of {self.list.count()} enabled")

    def disabled(self) -> set[str]:
        return {item.data(Qt.ItemDataRole.UserRole) for item in self._items()
                if item.checkState() != Qt.CheckState.Checked}


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
        self._disabled: dict[str, set[str]] = {}  # tag -> excluded source glyphs
        self._previewing_tag: str | None = None   # feature whose dialog is open
        self._last_font_folder = ""                # where "Add fonts…" last looked
        self._status_text = ("", "")               # preview status: before / after count
        self._auto_preview_text = ""           # last preview text the app filled in
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
        #   column 1, below:    options (renaming and PostScript name at the bottom)
        #   column 2, below:    OpenType features
        #   column 3:           preview and processing output
        files = self._build_files_side()
        options = self._build_options()
        features = self._build_features()
        output_side = self._build_output_side()
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(16)
        grid.addWidget(files, 0, 0, 1, 2)
        grid.addWidget(options, 1, 0)
        grid.addWidget(features, 1, 1)
        grid.addWidget(output_side, 0, 2, 2, 1)
        # Columns 1 and 2 share one width; column 3 (preview) is a quarter wider.
        for column, stretch in enumerate((4, 4, 5)):
            grid.setColumnStretch(column, stretch)
        grid.setRowStretch(0, 2)
        grid.setRowStretch(1, 3)
        # Ignore content widths so the columns stay equal at any window width.
        for widget in (files, options, features, output_side):
            policy = widget.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
            widget.setSizePolicy(policy)
        self._control_panels = [files, features, options]
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
        for signal in (self.suffix_check.toggled, self.suffix_edit.textChanged,
                       self.replacenames_edit.textChanged,
                       self.output_edit.textChanged):
            signal.connect(self._names_timer.start)

        self._set_theme(self._theme)
        self._update_file_count()
        self._rebuild_features()

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
        row.addWidget(all_button)
        row.addWidget(none_button)
        row.addStretch(1)
        box.addLayout(row)
        return section

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
        self.suffix_check = add_check("suffix")
        self.suffix_edit = QLineEdit()
        self.suffix_edit.setPlaceholderText("custom suffix, e.g. SC")
        self.suffix_edit.setEnabled(False)
        self.suffix_check.toggled.connect(self.suffix_edit.setEnabled)
        add_field(LABELS["usesuffix"], self.suffix_edit, "usesuffix")
        self.replacenames_edit = QLineEdit()
        self.replacenames_edit.setPlaceholderText("search1/replace1,search2/replace2")
        add_field(LABELS["replacenames"], self.replacenames_edit, "replacenames")
        self.info_check = add_check("info")
        # The selected font's names after processing, in their own small grid
        # spanning both columns, so the long labels do not widen the label
        # column above them.
        names = QGridLayout()
        names.setHorizontalSpacing(10)
        names.setVerticalSpacing(4)
        names.setColumnStretch(1, 1)
        self.family_name_label = QLabel("–")
        self.full_name_label = QLabel("–")
        self.postscript_name_label = QLabel("–")
        for index, (text, value) in enumerate((
                ("Family name of selected font", self.family_name_label),
                ("Full name of selected font", self.full_name_label),
                ("PostScript name of selected font", self.postscript_name_label))):
            label = _label(text, "hint")
            label.setWordWrap(False)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setToolTip("After processing")
            names.addWidget(label, index, 0)
            names.addWidget(value, index, 1)
        grid.setRowMinimumHeight(grid.rowCount(), 6)
        grid.addLayout(names, grid.rowCount(), 0, 1, 2)

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

        section, box, _ = _section("Preview")
        row = QHBoxLayout()
        self._hide_unchanged = False
        self.unchanged_button = QPushButton("Hide unchanged characters")
        self.unchanged_button.setToolTip(
            "Switch between the main characters of the selected font and only "
            "the characters the selected features change.")
        self.unchanged_button.clicked.connect(self._toggle_unchanged)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(8, 200)
        self.size_spin.setValue(32)
        self.size_spin.setSuffix(" pt")
        self.size_spin.valueChanged.connect(self._apply_preview_font)
        row.addWidget(self.unchanged_button)
        row.addStretch(1)
        row.addWidget(QLabel("Size"))
        row.addWidget(self.size_spin)
        box.addLayout(row)
        self.preview_edit = QTextEdit()
        self.preview_edit.setAcceptRichText(False)
        self.preview_edit.setPlaceholderText(
            "Add a font to see a preview. You can also type your own text here.")
        self.preview_edit.textChanged.connect(self._refresh_changed_highlight)
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
        self._label_duplicate_names()
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

        Fonts may have different features, but each one must contain every
        feature that is currently ticked.
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
        problems = self._missing_ticked(new)
        if problems:
            return ("These fonts lack OpenType features that are ticked:\n\n"
                    + self._describe_missing(problems)
                    + "\n\nNone of the fonts you just added were added to the "
                    "list. Untick those features first, or add other fonts.")
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
        self._names_timer.start()
        self._fonts_changed()

    def _clear_fonts(self) -> None:
        self.font_list.clear()
        self._selected_tags.clear()
        self._disabled.clear()
        self._fonts_changed()

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
            self._show_main_characters()
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
        if replacenames and any(p.count("/") != 1 for p in replacenames.split(",")):
            return None
        suffix = self.suffix_edit.text().strip() if self.suffix_check.isChecked() else ""
        return suffix, replacenames

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
        self.family_name_label.setText(selected.get("family") or "–")
        self.full_name_label.setText(selected.get("full") or "–")
        self.postscript_name_label.setText(selected.get("postscript") or "–")

    def _feature_toggled(self, checked: bool) -> None:
        tag = self.sender().property("tag")
        (self._selected_tags.add if checked else self._selected_tags.discard)(tag)
        self._schedule_preview()

    def _update_features_extra(self) -> None:
        shown = {tag for tag, _, _ in self._font_info["features"]}
        extra = sorted(self._selected_tags - shown)
        self.features_extra.setText(
            "Also selected, but not in this font: " + ", ".join(extra))
        self.features_extra.setVisible(bool(extra))

    def _rebuild_features(self) -> None:
        checked = self._selected_tags
        self._feature_boxes = []
        container = QWidget()
        grid = QGridLayout(container)
        grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        features = self._font_info["features"]
        if not features:
            text = ("This font has no GSUB features." if self._active_font
                    else "Add a font to list its OpenType features.")
            grid.addWidget(_label(text, "hint"), 0, 0)
        rows = -(-len(features) // FEATURE_COLUMNS)
        for index, (tag, name, pairs) in enumerate(features):
            count = ""
            if pairs:
                tip = ""
            else:
                count = "not freezable"
                tip = ("This feature has no single or alternate substitutions "
                       "(it uses ligatures or contextual rules), so freezing "
                       "it will not change the font.")
            lacking = [os.path.basename(path) for path in self.font_paths()
                       if tag not in self._tags(path)]
            if lacking:
                count += ("  ·  " if count else "") + "not in all fonts"
                tip += ("\n\n" if tip else "") + "Not in: " + ", ".join(lacking[:10]) + (
                    f" and {len(lacking) - 10} more" if len(lacking) > 10 else "")
            box = QCheckBox("  ·  ".join(part for part in (tag, name, count) if part))
            box.setObjectName("feature")
            box.setProperty("tag", tag)
            box.setChecked(tag in checked)
            box.setToolTip(tip)
            box.setEnabled(bool(pairs))  # greyed out when it cannot be frozen
            box.toggled.connect(self._feature_toggled)
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(box)
            row_layout.addStretch(1)
            if pairs:
                row_layout.addWidget(self._substitutions_button(tag, name, pairs))
            grid.addWidget(row, index % rows, index // rows)
            self._feature_boxes.append(box)
        self.features_area.setWidget(container)
        self.features_title.setText("OpenType features")
        self._update_features_extra()

    def _substitutions_button(self, tag: str, name: str, pairs: dict) -> QPushButton:
        total = len(pairs)
        off = len(self._disabled.get(tag, set()) & set(pairs))
        unit = "glyph" if total == 1 else "glyphs"
        button = QPushButton(f"{total - off} of {total} {unit}" if off
                             else f"{total} {unit}")
        button.setObjectName("small")
        button.setProperty("partial", bool(off))
        button.setToolTip("Show this feature's glyph substitutions and choose "
                          "which ones to freeze")
        button.setAccessibleName(f"{tag} substitutions")
        button.clicked.connect(
            lambda: self._edit_substitutions(tag, name, pairs))
        return button

    def _edit_substitutions(self, tag: str, name: str, pairs: dict) -> None:
        if not self._active_font:
            return
        original = set(self._disabled.get(tag, set()))
        # Keep exclusions of glyphs this font lacks (they may be in other fonts).
        kept = original - set(pairs)

        def apply(disabled: set[str]) -> None:
            if disabled:
                self._disabled[tag] = set(disabled)
            else:
                self._disabled.pop(tag, None)

        dialog = SubstitutionDialog(
            self, self._active_font, tag, name, pairs, self._font_info["cmap"],
            original)
        # Every tick or untick updates the preview straight away; the feature
        # is previewed even if it is not ticked in the feature list.
        dialog.changed.connect(lambda disabled: (apply(kept | disabled),
                                                 self._schedule_preview()))
        self._previewing_tag = tag
        self._place_beside_preview(dialog)
        self._update_preview()
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self._previewing_tag = None
        apply(kept | dialog.disabled() if accepted else original)  # Cancel restores
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
                    family, self._disabled)
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
        # Collect runs of adjacent changed characters and colour each run as
        # one range: Qt drops every other one of many back-to-back ranges.
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
        selections = []
        for start, end in runs:
            selection = QTextEdit.ExtraSelection()
            cursor = QTextCursor(self.preview_edit.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            selection.cursor = cursor
            selection.format.setForeground(color)
            selections.append(selection)
        self.preview_edit.setExtraSelections(selections)

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
        self.preview_edit.setPlainText(text)

    def _show_main_characters(self) -> None:
        self.preview_edit.setPlaceholderText(
            "Add a font to see a preview. You can also type your own text here.")
        self._fill_preview(main_characters(
            self._font_info["cmap"], self._font_info["ligatures"]))

    def _show_changed_characters(self) -> None:
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
            self._show_main_characters()

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
        if replacenames and any(
                pair.count("/") != 1 for pair in replacenames.split(",")):
            return fail(
                "Replace names must be written as search/replace pairs separated "
                "by commas, for example:\n\nInter/Outer,Display/Headline")
        use_suffix = self.suffix_check.isChecked()
        return {
            "output": output,
            "features": ",".join(self.selected_features()),
            "script": pad_tag(self.script_combo.currentText()) or None,
            "lang": pad_tag(self.lang_combo.currentText()) or None,
            "zapnames": self.zapnames_check.isChecked(),
            # The family name gets the custom suffix only, never feature tags.
            "suffix": bool(use_suffix and self.suffix_edit.text().strip()),
            "usesuffix": self.suffix_edit.text().strip() if use_suffix else "",
            "replacenames": replacenames,
            "info": self.info_check.isChecked(),
            "report": self.report_check.isChecked(),
            "names": self.names_check.isChecked(),
            "verbose": self.verbose_check.isChecked(),
            "disabled": {tag: set(glyphs) for tag, glyphs in self._disabled.items()},
        }

    def _generate(self) -> None:
        if self._worker is not None:
            return
        settings = self._collect_settings()
        if settings is None:
            return
        paths = self.font_paths()
        folder = settings.pop("output")
        problems = self._missing_ticked(paths)
        if problems:
            self._show_error(
                "Cannot generate fonts",
                "Every input font must contain the ticked OpenType features. "
                "These fonts lack some of them:\n\n"
                + self._describe_missing(problems)
                + "\n\nUntick those features or remove those fonts.")
            return

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
        if overwrite:
            self._log("Overwriting the original font files: "
                      + ", ".join(os.path.basename(path) for path in clashes))
        if not settings["features"]:
            self._log("Note: no features are selected, so no glyphs will be remapped.")
        excluded = {tag: glyphs for tag, glyphs in settings["disabled"].items()
                    if tag in self._selected_tags}
        for tag, glyphs in sorted(excluded.items()):
            self._log(f"Excluded from {tag}: " + ", ".join(sorted(glyphs)))
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
