"""The calibration target reader: the agent's tool that reads the owner's photo of both calibration
targets (decisions 0026, 0027; owner, 2026-10-13: "its easier for you (an LLM) to deal with bad
picture quality", then "build the calibration targets"; 2026-10-14: "Start working on a v2 that
isn't so picky"). Never a Central feature, never shipped: run on the agent's Mac as
`python -m scripts.calibration_target PHOTO` (the runbook and the display-control skill say how).

Modules, dependencies pointing down (held by tests/test_calibration_target_reader_imports.py):

| Module | Owns | May import |
|---|---|---|
| `__main__` | the command line: decode, locate, fetch the entry, read, suggest, print JSON | every module here |
| `photos`, `central_reads` | the edges: a photo file to a linear `Photo` (rawpy for ProRAW DNG, Pillow and pillow-heif for normal photos); Central's sent entry (the operator route) | `model` |
| `suggest` | what to change, by stage | `model`, `player.geometry`, `appliance.display_host.picture` (`operations_for`), `contracts` |
| `read` | a Reading from a located photo and its sent entry: squares, whites over their blacks, the ink, the lens falloff, the vote, greys and rulers | `locate`, `model`, `contracts` |
| `locate` | both boards (OpenCV's ChArUco detector and USAC homography), the code (its QR detector), the viewing angle (solvePnP) | `boards`, `model`, `contracts` |
| `boards` | both boards as OpenCV objects, built from the contracts' constants | `contracts` |
| `model` | the vocabulary and every threshold | `contracts`, `central.displays.views` |

`read`, `locate`, `boards`, `suggest` and `model` do no I/O (no file, HTTP or database), so the
reader is proved on synthetic photos (tests/calibration_target_scenes.py). The real-photo check is
a bench step (NOT RUN in CI).
"""
