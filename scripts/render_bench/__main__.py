"""`python -m scripts.render_bench --output HDMI-A-1 --photo P1 --photo P2 --photo P3 --photo P4
--video V.mp4 --out DIR [--soak SECONDS] [--budget unlimited]`: run the tracer (bead B6) and write
DIR/moments.jsonl and DIR/verdict.json; exit 0 when the verdict passes, 1 when it fails, 2 when the
bench could not run. `--budget unlimited` gives the renderer a budget it never meets (the budget
mutation probe's run)."""
from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    """Run the bench (module docstring)."""
    raise NotImplementedError("render-planes S1 bead B6")


if __name__ == "__main__":
    raise SystemExit(main())
