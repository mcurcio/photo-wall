"""Check repository-relative Markdown file links without fetching external sites."""

import re
from pathlib import Path
from urllib.parse import unquote

root = Path(__file__).resolve().parents[1]
documents = [*root.glob("*.md"), *root.joinpath("docs").rglob("*.md")]
failures = []
for document in documents:
    content = re.sub(r"```.*?```", "", document.read_text(), flags=re.S)
    for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", content):
        target = target.strip("<>").split("#", 1)[0]
        if not target or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
            continue
        if not (document.parent / unquote(target)).exists():
            failures.append(f"{document.relative_to(root)}: {target}")
if failures:
    raise SystemExit("Broken documentation links:\n" + "\n".join(failures))
print(f"Checked relative file links in {len(documents)} Markdown documents.")
