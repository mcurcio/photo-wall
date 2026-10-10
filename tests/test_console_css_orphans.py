"""The legacy stylesheet keeps no rule for a class nothing renders (console design system
migration rule: a page's old CSS goes in the same bead that deletes the page).

A class selector in central/console/src/index.css must have a user in the console's JS, JSX,
TS or TSX: the class's name as a word, or its prefix before a `--` modifier written as a
template (`` `health--${tier}` ``). A class built any other way must be written out whole.
"""

import re
from pathlib import Path

SRC = Path(__file__).parents[1] / "central/console/src"
SOURCES = (".js", ".jsx", ".ts", ".tsx")


def _selector_classes(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    classes = set()
    for selector in re.findall(r"([^{}]+)\{", css):
        if not selector.strip().startswith("@"):
            classes |= set(re.findall(r"\.([A-Za-z_][\w-]*)", selector))
    return classes


def orphans(css, source):
    words = set(re.findall(r"[\w-]+", source))
    return sorted(name for name in _selector_classes(css)
                  if name not in words and not (
                      "--" in name and name.rsplit("--", 1)[0] + "--${" in source))


def test_every_class_in_the_legacy_stylesheet_has_a_user():
    source = "\n".join(path.read_text() for path in SRC.rglob("*") if path.suffix in SOURCES)
    assert orphans((SRC / "index.css").read_text(), source) == []


def test_the_orphan_check_finds_a_class_nothing_renders():
    css = ".kept { color: red }\n.gone { color: red }\n.tier--ok, .tier--bad { color: red }\n"
    source = 'h("p", { className: "kept" }); `tier--${tier}`'
    assert orphans(css, source) == ["gone"]
