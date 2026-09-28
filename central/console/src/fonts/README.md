# Console Sans

`ConsoleSans.woff2` is a subset of the Google Sans variable font, renamed. It is the console's text face ([pass C §5](../../../../docs/operator-console-ux-pass2-flow.md#5-look-and-feel-pass-c)).

- **Source:** [google/fonts `ofl/googlesans`](https://github.com/google/fonts/tree/main/ofl/googlesans), file `GoogleSans[GRAD,opsz,wght].ttf`, fetched 2026-09-28 from `raw.githubusercontent.com/google/fonts/main/ofl/googlesans/`. The upstream font reports `Version 14.000;[08f2ac800]`. The raw endpoint gives no commit, so [`build_font.py`](build_font.py) pins the SHA-256 of the exact bytes (`d0a87d83…a3a90a` for the font, `2b75ef20…4fb958` for `OFL.txt`) and refuses anything else.
- **Licence:** SIL Open Font License 1.1, in [`OFL.txt`](OFL.txt), copied unchanged. The build emits it into `dist/assets/` beside the font (`main.jsx`). The font keeps its copyright notice (name ID 0) and the licence description and URL (IDs 13 and 14).
- **Why the rename:** Google's [TRADEMARKS.md](https://github.com/google/fonts/blob/main/ofl/googlesans/TRADEMARKS.md) allows the "Google Sans" mark on a Modified Version only in connection with the original font. A subset is a Modified Version, so every naming record (IDs 1, 3, 4 and 6; 16, 17 and 25 when present; the `fvar` and `STAT` names) reads "Console Sans" or "ConsoleSans". Only the copyright notice, which the OFL requires to be kept, still names the Google Sans Project Authors.
- **Recipe:** instance with GRAD 0 and opsz 18, keeping wght 400–700; subset to Latin-1, common punctuation, the arrows and the three status shapes (■ ▲ ●); rename; save as WOFF2. The steps and their exact parameters are in [`build_font.py`](build_font.py).
- **Rebuild:** from the repository root, `uv run --no-project --with 'fonttools[woff]==4.66.0' python central/console/src/fonts/build_font.py`.
- **Measured size:** 28,712 bytes (28.0 KiB) with fontTools 4.66.0.
- **Checked by:** `tests/test_console_look.py` (names, licence records and axes) and `tests/browser/test_console_look_browser.py` (served type, loaded face, same-origin requests).
