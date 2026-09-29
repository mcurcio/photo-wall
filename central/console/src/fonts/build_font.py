"""Rebuild ConsoleSans.woff2 from the upstream Google Sans variable font (pass C §5).

Run from the repository root:

    uv run --no-project --with 'fonttools[woff]==4.66.0' \
        python central/console/src/fonts/build_font.py

It downloads the pinned upstream files, checks their SHA-256, and writes
ConsoleSans.woff2 and OFL.txt beside this script. The recipe:

1. Instance: pin GRAD=0 and opsz=18, keep wght 400-700 (fontTools.varLib.instancer).
2. Subset: Latin-1 plus common punctuation and the arrows and status shapes the console
   uses, keeping name IDs 0-6, 13 and 14 (copyright, names, licence description and URL).
3. Rename the family to "Console Sans" in every naming record (IDs 1, 3, 4, 6, 16, 17 and
   25), because TRADEMARKS.md limits the "Google Sans" mark on Modified Versions and a
   subset is one. The copyright notice (ID 0) is kept verbatim, as the OFL requires.
4. Save as WOFF2.
"""

import hashlib
import sys
import tempfile
import urllib.request
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

UPSTREAM = "https://raw.githubusercontent.com/google/fonts/main/ofl/googlesans/"
SOURCES = {
    # file name upstream -> SHA-256 of the bytes this recipe was measured on (2026-09-28)
    "GoogleSans[GRAD,opsz,wght].ttf":
        "d0a87d835a944b8b40d0e82a5651bb59ab97b936a2aeed5946eb57e7b2a3a90a",
    "OFL.txt": "2b75ef20f13d83a7514aee452c4782c20cdc9ff2dee17600f44d37a06d4fb958",
}

FAMILY = "Console Sans"
POSTSCRIPT_FAMILY = "ConsoleSans"
UPSTREAM_FAMILY = "Google Sans"
UPSTREAM_POSTSCRIPT_FAMILY = "GoogleSans"

# Latin-1, common punctuation, and the non-Latin-1 characters the console renders:
# dashes, quotes, bullet, ellipsis, primes, euro, trademark, arrows, minus, and the
# status-chip shapes (■ ▲ ●). A code point the font lacks is simply skipped.
UNICODES = (
    "U+0020-007E,U+00A0-00FF,U+2013-2014,U+2018-201A,U+201C-201E,U+2020-2022,U+2026,"
    "U+2030,U+2032-2033,U+2039-203A,U+2044,U+20AC,U+2122,U+2190-2194,U+2212,"
    "U+25A0,U+25B2,U+25CF"
)
NAME_IDS = [0, 1, 2, 3, 4, 5, 6, 13, 14]
# Records that name the font; each must read "Console Sans" once the rename is done.
RENAMED_IDS = (1, 3, 4, 6, 16, 17, 25)

HERE = Path(__file__).resolve().parent


def fetch(name: str) -> bytes:
    url = UPSTREAM + urllib.request.quote(name)
    with urllib.request.urlopen(url) as response:  # noqa: S310 - fixed https URL
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SOURCES[name]:
        sys.exit(f"{name}: SHA-256 {digest} is not the pinned {SOURCES[name]}; "
                 "upstream changed, so re-measure and update the pin deliberately")
    return data


def rename(font: TTFont) -> None:
    for record in font["name"].names:
        if record.nameID in RENAMED_IDS:
            text = record.toUnicode()
            text = text.replace(UPSTREAM_FAMILY, FAMILY)
            text = text.replace(UPSTREAM_POSTSCRIPT_FAMILY, POSTSCRIPT_FAMILY)
            # Unique ID: drop the upstream vendor token ("14.000;GOOG;…").
            text = text.replace(";GOOG;", ";")
            record.string = text


def build(source: Path, out: Path) -> None:
    instanced = source.with_name("instanced.ttf")
    font = instancer.instantiateVariableFont(
        TTFont(source), {"GRAD": 0, "opsz": 18, "wght": (400, 700)}
    )
    # Save and reopen between steps, as the two command-line tools would: the subsetter
    # cannot read the instancer's in-memory gvar.
    font.save(instanced)
    font = TTFont(instanced)
    options = subset.Options()
    options.name_IDs = NAME_IDS
    options.notdef_outline = True
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=subset.parse_unicodes(UNICODES))
    subsetter.subset(font)
    rename(font)
    font.flavor = "woff2"
    font.save(out)


def main() -> None:
    ttf = fetch("GoogleSans[GRAD,opsz,wght].ttf")
    (HERE / "OFL.txt").write_bytes(fetch("OFL.txt"))
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.ttf"
        source.write_bytes(ttf)
        out = HERE / "ConsoleSans.woff2"
        build(source, out)
    print(f"{out.name}: {out.stat().st_size} bytes")


if __name__ == "__main__":
    main()
