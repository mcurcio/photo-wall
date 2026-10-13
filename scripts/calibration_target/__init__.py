"""The calibration target reader: the agent's tool that reads the owner's photos of both calibration
targets (decision 0026; owner, 2026-10-13: "its easier for you (an LLM) to deal with bad picture
quality", then "build the calibration targets"). Never a Central feature, never shipped: run on the
agent's Mac as `python -m scripts.calibration_target PHOTO [PHOTO]` (the runbook and the
display-control skill say how).

Modules, dependencies pointing down (held by tests/test_calibration_target_reader_imports.py):

| Module | Owns | May import |
|---|---|---|
| `__main__` | the command line: decode, locate, fetch the entry, read, suggest, print JSON | every module here |
| `photos`, `central_reads` | the edges: a photo file to `Photo` (rawpy for ProRAW DNG, Pillow and pillow-heif for normal photos); Central's sent entry (the operator route) and a LUT's table (read-only SQL) | `model` |
| `suggest` | what to change, by stage | `model`, `player.geometry`, `appliance.display_host.picture` (`operations_for`), `contracts` |
| `read` | a Reading from located photos and their sent entry | `model`, `contracts` |
| `locate` | markers, the code, the scale check (OpenCV's ArUco and QR detectors) | `model`, `contracts` |
| `model` | the vocabulary and every threshold | `contracts`, `central.displays.views` |

`read`, `locate`, `suggest` and `model` do no I/O (no file, HTTP or database), so the review's
attacks are proved on synthetic photos (tests/calibration_target_scenes.py). The real-photo check
is a bench step (NOT RUN in CI).
"""
