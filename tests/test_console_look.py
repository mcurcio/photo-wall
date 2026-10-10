"""The console's look (pass C §5): token contrast, the font file, and the build config.

No browser: the contrast pairs are computed from the design tokens (design/tokens.css: the
dark `@theme` block and its `@variant light` override) with the WCAG 2.2 relative-luminance
formula, the status tints the legacy stylesheet (index.css) mixes are read from it, the font is read with fontTools, and the Vite config
is read as text. The browser half is tests/browser/test_console_look_browser.py.
"""

import re
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

CONSOLE = Path(__file__).resolve().parents[1] / "central" / "console"
TOKENS = CONSOLE / "src" / "design" / "tokens.css"
CSS = CONSOLE / "src" / "index.css"
FONT = CONSOLE / "src" / "fonts" / "ConsoleSans.woff2"
VITE_CONFIG = CONSOLE / "vite.config.js"

SCHEMES = ("light", "dark")
STATUSES = ("ok", "todo", "notice", "alarm", "unknown")
CHIP_MIX = 0.12  # a chip's status colour over --color-surface-raised (§5)
TRUTH_KINDS = ("set", "reported", "claimed", "derived", "planned", "unknown")  # facts.js
TEXT, NON_TEXT = 4.5, 3.0  # WCAG 1.4.3 (AA) and 1.4.11


# --- tokens -----------------------------------------------------------------------------

def _declarations(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))


def _tokens() -> dict[str, dict[str, str]]:
    """Both schemes' colour tokens, resolved to values, keyed without `--color-`.

    tokens.css has one `@theme` block for the dark scheme and one `:root { @variant light
    {…} }` override for the light one.
    """
    css = re.sub(r"/\*.*?\*/", "", TOKENS.read_text(), flags=re.S)
    theme = re.search(r"@theme\b[^{]*\{(.*?)\}", css, flags=re.S)
    assert theme, "tokens.css has no @theme block"
    light = re.search(r"@variant\s+light\s*\{(.*?)\}", css, flags=re.S)
    assert light, "tokens.css has no light override"
    dark = _declarations(theme.group(1))
    return {
        "dark": _colours(_resolve(dark)),
        "light": _colours(_resolve({**dark, **_declarations(light.group(1))})),
    }


def _colours(tokens: dict[str, str]) -> dict[str, str]:
    return {f"--{name[len('--color-'):]}": value for name, value in tokens.items()
            if name.startswith("--color-") and value != "initial"}


def _resolve(tokens: dict[str, str]) -> dict[str, str]:
    def value(name: str, seen: tuple[str, ...] = ()) -> str:
        assert name not in seen, f"token cycle through {name}"
        raw = tokens[name].strip()
        ref = re.fullmatch(r"var\((--[\w-]+)\)", raw)
        return value(ref.group(1), (*seen, name)) if ref else raw

    return {name: value(name) for name in tokens}


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    digits = hex_colour.lstrip("#")
    assert re.fullmatch(r"[0-9a-fA-F]{3}|[0-9a-fA-F]{6}", digits), hex_colour
    if len(digits) == 3:
        digits = "".join(d * 2 for d in digits)
    return tuple(int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _luminance(rgb: tuple[float, float, float]) -> float:
    def linear(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _mix(colour, base, weight):
    """`color-mix(in srgb, colour weight, base)`: per-channel interpolation in sRGB."""
    return tuple(weight * c + (1 - weight) * b for c, b in zip(colour, base))


def _status_tints() -> set[float]:
    """Every weight at which index.css tints a surface with a status colour
    (`color-mix(in srgb, var(--color-notice) 15%, …)`): the status text on it must read."""
    css = CSS.read_text()
    weights = re.findall(
        r"color-mix\(in srgb,\s*var\(--color-(?:" + "|".join(STATUSES) + r")\)\s*(\d+)%", css)
    return {int(weight) / 100 for weight in weights}


def _accent_tint(tokens: dict[str, str]) -> float:
    """The weight of `--accent-tint` (`color-mix(in srgb, var(--color-accent) N%,
    transparent)`)."""
    match = re.fullmatch(r"color-mix\(in srgb,\s*var\(--color-accent\)\s*(\d+)%,\s*transparent\)",
                         tokens["--accent-tint"].strip())
    assert match, tokens["--accent-tint"]
    return int(match.group(1)) / 100


def _pairs(tokens: dict[str, str]):
    """Every (description, foreground, background, minimum) pair §5 names."""
    c = {name[2:]: _rgb(v) for name, v in tokens.items() if v.startswith("#")}
    # Composite pairs: a tint over transparent shows the surface under it, so its colour
    # is the tint mixed over that surface (--surface or --surface-raised).
    accent_tint = _accent_tint(tokens)
    for surface in ("surface", "surface-raised"):
        tint = _mix(c["accent"], c[surface], accent_tint)
        yield f"--accent text on --accent-tint over --{surface}", c["accent"], tint, TEXT
        for status in STATUSES:
            yield (f"--{status} text on --accent-tint over --{surface}", c[status], tint, TEXT)
            for weight in sorted(_status_tints()):
                yield (f"--{status} text on its {weight:.0%} tint over --{surface}", c[status],
                       _mix(c[status], c[surface], weight), TEXT)
    surfaces = ("surface", "surface-raised", "surface-input")
    for text in ("text", "muted", "label", *(f"truth-{kind}" for kind in TRUTH_KINDS)):
        for surface in surfaces:
            yield f"--{text} on --{surface}", c[text], c[surface], TEXT
    yield "--on-accent on --accent", c["on-accent"], c["accent"], TEXT
    yield "--on-alarm on --alarm", c["on-alarm"], c["alarm"], TEXT
    for status in STATUSES:
        chip = _mix(c[status], c["surface-raised"], CHIP_MIX)
        yield f"--text on the --{status} chip", c["text"], chip, TEXT
        yield f"--{status} border on --surface-raised", c[status], c["surface-raised"], NON_TEXT
        # Status colours are also used as text today (reasons, errors, health labels).
        for surface in ("surface", "surface-raised"):
            yield f"--{status} text on --{surface}", c[status], c[surface], TEXT
    for surface in surfaces:
        yield f"--line-input on --{surface}", c["line-input"], c[surface], NON_TEXT
    for surface in ("surface", "surface-raised"):
        yield f"--focus on --{surface}", c["focus"], c[surface], NON_TEXT
        yield f"--accent text on --{surface}", c["accent"], c[surface], TEXT


def test_both_schemes_define_the_semantic_tokens():
    tokens = _tokens()
    assert set(tokens) == set(SCHEMES)
    required = {
        "--surface", "--surface-raised", "--surface-sunken", "--surface-input", "--text",
        "--muted", "--label", "--line", "--line-input", "--accent", "--accent-tint",
        "--on-accent", "--focus", "--on-alarm", "--shadow",
        *(f"--{status}" for status in STATUSES), *(f"--truth-{kind}" for kind in TRUTH_KINDS),
    }
    for scheme in SCHEMES:
        assert required <= set(tokens[scheme]), (scheme, required - set(tokens[scheme]))


@pytest.mark.parametrize("scheme", SCHEMES)
def test_token_pairs_meet_wcag_contrast(scheme):
    failures = [
        f"{what}: {ratio:.2f} < {minimum}"
        for what, fg, bg, minimum in _pairs(_tokens()[scheme])
        if (ratio := _ratio(fg, bg)) < minimum
    ]
    assert not failures, f"{scheme} scheme:\n" + "\n".join(failures)


def test_the_status_tints_are_read_from_the_stylesheet():
    # Guards the composite pairs: the tints index.css uses today are among them.
    assert {0.12, 0.15} <= _status_tints()


def test_contrast_formula_matches_known_values():
    # Guards the checker itself: black on white is 21:1, and #737373 on white is 4.74:1.
    assert _ratio(_rgb("#000"), _rgb("#fff")) == pytest.approx(21.0)
    assert _ratio(_rgb("#737373"), _rgb("#ffffff")) == pytest.approx(4.74, abs=0.01)


# --- font -------------------------------------------------------------------------------

UPSTREAM_NAMES = ("Google Sans", "GoogleSans")
FAMILY = "Console Sans"


@pytest.fixture(scope="module")
def font():
    return TTFont(FONT)


def _names(font, *ids):
    return [r.toUnicode() for r in font["name"].names if r.nameID in ids]


def _referenced_name_ids(font) -> set[int]:
    """The name IDs that the fvar and STAT tables point at."""
    ids = set()
    fvar = font["fvar"]
    for axis in fvar.axes:
        ids.add(axis.axisNameID)
    for instance in fvar.instances:
        ids.update({instance.subfamilyNameID, instance.postscriptNameID} - {0xFFFF})
    stat = font["STAT"].table
    ids.add(stat.ElidedFallbackNameID)
    ids.update(axis.AxisNameID for axis in stat.DesignAxisRecord.Axis)
    if stat.AxisValueArray:
        ids.update(value.ValueNameID for value in stat.AxisValueArray.AxisValue)
    return ids


def test_font_family_is_renamed_to_console_sans(font):
    assert _names(font, 1) and all(n == FAMILY for n in _names(font, 1))
    assert all(n.startswith(FAMILY) for n in _names(font, 4, 16))
    assert all(n.startswith("ConsoleSans") for n in _names(font, 6, 25))
    assert _names(font, 3) and all("ConsoleSans" in n for n in _names(font, 3))


def test_no_name_record_carries_the_upstream_mark(font):
    # TRADEMARKS.md: a Modified Version may not be named "Google Sans". The copyright
    # notice (ID 0) is the one exception: the OFL requires it to be kept verbatim.
    offending = [
        (r.nameID, r.toUnicode()) for r in font["name"].names
        if r.nameID != 0 and any(mark in r.toUnicode() for mark in UPSTREAM_NAMES)
    ]
    assert not offending
    present = {r.nameID for r in font["name"].names}
    assert _referenced_name_ids(font) <= present, "fvar/STAT point at a dropped name"
    assert all(n.startswith("Copyright") for n in _names(font, 0))


def test_font_keeps_its_licence_records(font):
    assert any("SIL Open Font License" in n for n in _names(font, 13))
    assert any(n.startswith("https://openfontlicense.org") for n in _names(font, 14))
    assert (FONT.parent / "OFL.txt").read_text().find("SIL OPEN FONT LICENSE") >= 0


def test_font_varies_only_in_weight(font):
    assert font.flavor == "woff2"
    axes = {a.axisTag: (a.minValue, a.maxValue) for a in font["fvar"].axes}
    assert axes == {"wght": (400.0, 700.0)}


# --- build config -----------------------------------------------------------------------

def test_vite_build_never_inlines_assets_as_data_urls():
    # The CSP (`default-src 'self'`) refuses data: URLs, so every asset must be a file.
    config = re.sub(r"//[^\n]*", "", VITE_CONFIG.read_text())
    assert re.search(r"\bbuild\s*:\s*\{.*?\bassetsInlineLimit\s*:\s*0\s*[,}\n]", config, re.S)
