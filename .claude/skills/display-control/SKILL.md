---
name: display-control
description: Operate a Photo Wall Pi's displays by hand over SSH - read a display's EDID, map HDMI ports to CEC devices and DDC/CI buses, turn a display off and on with cec-ctl or ddcutil, ask a display for the picture list (`getvcp <codes> --terse`, `capabilities`) and read and set its picture settings (brightness VCP 10, contrast VCP 12, colour preset 14, gains…), and read what the display controller did. Read the HDMI signal range (Broadcast RGB, is_limited_range, the AVI InfoFrame) and whether a display honours it (the EDID's quantization-selectable bit). Use it for the 1b bench step (Test off and on), the display-tuning bench steps (Full and Limited on a TV, the time without photos per change), the 1c and picture-split bench steps (display and Photo Wall brightness from the console read back, which codes a display answers, the time of an ask, power during an ask, Cover on the test monitor, write timing), to diagnose "Display didn't answer", "The display did not take it", "Checking what this display offers" that never clears or a power method that never answers, or to probe a new display's support. Also, when the owner sends a photo of a Frame while tuning, read the picture and Position values Central sent it at that time (configuration snapshots); when he AirDrops ProRAW photos of the calibration targets, read them with the reader script and tell him what to change. The rules (what the controller does, what Central decides) live in the docs it links; this is the runbook and its traps.
---

# display-control

How to reach and drive the displays on a Photo Wall Pi 5, learned in the 1b spike on the test Pi
(2026-10-10), the 1b build, the 1c build, the picture split ([0022](../../../docs/decisions/0022-picture-hardware-and-software-apart.md)) and the display-tuning probe on the owner's Samsung QN43Q8F (2026-10-13, [0024](../../../docs/decisions/0024-display-tuning.md)), configuration snapshots (2026-10-13, [0025](../../../docs/decisions/0025-configuration-snapshots.md)) and the calibration targets (2026-10-13, [0026](../../../docs/decisions/0026-calibration-targets.md)). The rules are not here; they have one home each:

| Rules | Home |
|---|---|
| What the Pi's display controller does (when it acts, read before write, best detected, the Output document and report) | [Player architecture › Display identity and power](../../../docs/player-architecture.md#display-identity-and-power-1b) |
| Picture on the Pi (the two keys, asking the picture list, power first on the port, write order, newest wins, picture operations, frame protocol v4) | [Player architecture › Picture settings](../../../docs/player-architecture.md#picture-settings-1c) |
| How Central recognises a display, readiness, the Power and Picture tabs' words, Copy | [Operator console design §5–§7](../../../docs/operator-console-design.md#6-display-identity) |
| Central's side: tables, routes, the LISTEN wake, what each console result means | [Runbook › Display identity and power](../../../docs/runbook.md#display-identity-and-power) |
| The signal range and test patterns (what each is, Done and Revert, set before Weston starts, how to tune a display with no controls) | [0024](../../../docs/decisions/0024-display-tuning.md), [Display host backend › Units](../../../docs/display-host-backend.md#units-and-weston-incarnations), [Runbook › Tune a display with no controls](../../../docs/runbook.md#tune-a-display-with-no-controls) |
| What the picture and Position were at a time (configuration snapshots: the kinds, Restore, the routes, tables and SQL) | [0025](../../../docs/decisions/0025-configuration-snapshots.md), [Runbook › Configuration snapshots](../../../docs/runbook.md#configuration-snapshots); `GET /v1/operator/frames/{id}/tuning-snapshot?at=`, `GET /v1/operator/displays/{id}/picture-history`, `GET /v1/operator/frames/{id}/position-history` |
| The calibration targets (both stages, what each aims at, the layout, the reader's rules) | [0026](../../../docs/decisions/0026-calibration-targets.md), [Runbook › Calibration targets](../../../docs/runbook.md#calibration-targets); the layout `contracts/calibration_target.py`, the reader's thresholds `scripts/calibration_target/model.py` |
| SSH access to a Node | [Runbook › Reaching a Node over SSH](../../../docs/runbook.md#reaching-a-node-over-ssh) |
| The code | `appliance/display_host/ddc.py` (the one place `ddcutil` runs), `power_methods.py` (the exact power tool calls), `picture_list.py` (the ask), `picture.py` (writes and the base page line), `signal_range.py` (the connector's range), `output_power.py`, `edid.py`; `contracts/node_output.py`, `contracts/node_picture.py` (the picture list and its names) |

## Access

```sh
ssh -i ~/.ssh/photo_wall_agent -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null photowall@<pi-ip>
```

`sudo` needs no password and prints a harmless "unable to resolve host" warning. The host key
changes at every boot. Find the IP from the router's lease table or `nmap -p 22 --open <subnet>`;
the test Pi is serial `75628d0c` (`photo-wall-75628d0c`). The journal is in RAM: read it before any
reboot.

## What is on the image

From the first release that carries 1b: `ddcutil` and `cec-ctl` (package `v4l-utils`) in
`/usr/bin`, and the DDC/CI driver `i2c-dev` loaded at boot by systemd from
`/etc/modules-load.d/photo-wall-ddc.conf`. Check before anything else:

```sh
ls /usr/bin/ddcutil /usr/bin/cec-ctl
ls /sys/module/i2c_dev && ls -l /dev/i2c-* /dev/cec*   # expect i2c-13, i2c-14 (root:i2c 0660), cec0, cec1 (root:video 0660)
```

No `/sys/module/i2c_dev` means a release from before 1b (use the spike recipe at the end).

## Map the ports first

| Output | DRM connector | DDC/CI bus | CEC device |
|---|---|---|---|
| `HDMI-A-1` | `card1-HDMI-A-1` | 13 | `cec0` |
| `HDMI-A-2` | `card1-HDMI-A-2` | 14 | `cec1` |

Confirm rather than trust the table:

```sh
readlink /sys/class/drm/card1-HDMI-A-1/ddc                  # .../i2c-13
cat /sys/class/drm/card1-HDMI-A-*/status                    # connected / disconnected
sudo cec-ctl -d /dev/cec0                                    # "DRM Connector Info" and the physical address; f.f.f.f = nothing plugged in
```

## Identity (EDID)

The EDID is world-readable; no tool is needed and `edid-decode` is not on the image.

```sh
od -An -tx1 /sys/class/drm/card1-HDMI-A-1/edid | head -8     # bytes 8-9 maker, 10-11 product (little-endian), 12-15 serial
sudo journalctl -b --no-pager _PID=$(pgrep -x weston) | grep EDID   # Weston's reading: make, model, serial
```

The test monitor reads maker `XYM`, product 5475, name `MNN`, serial blank and zero: no usable
serial, so Central recognises it by make and model on its Frame.

## HDMI-CEC

```sh
sudo cec-ctl -d /dev/cec0 --playback -S                      # claim a logical address and scan
sudo cec-ctl -d /dev/cec0 --to 0 --give-device-power-status  # read
sudo cec-ctl -d /dev/cec0 --to 0 --standby                   # off; --image-view-on for on
sudo cec-ctl -d /dev/cec0 --clear                            # ALWAYS, when done
```

`Tx, Not Acknowledged (4), Max Retries` means no CEC TV is listening: the controller reports
"Display didn't answer". The test monitor has no CEC.

## DDC/CI

Always `--noconfig` and a cache directory under `/tmp`, and always read back after a write:

```sh
sudo env XDG_CACHE_HOME=/tmp/ddc ddcutil --noconfig detect
sudo env XDG_CACHE_HOME=/tmp/ddc ddcutil --noconfig --bus 13 getvcp 10 12 60 D6 --terse
sudo env XDG_CACHE_HOME=/tmp/ddc ddcutil --noconfig --bus 13 setvcp D6 4     # standby; reads back x02
sudo env XDG_CACHE_HOME=/tmp/ddc ddcutil --noconfig --bus 13 setvcp D6 1     # on; reads back x01
rm -rf /tmp/ddc
```

VCP codes: `10` brightness, `12` contrast, `14` colour preset, `60` input, `D6` power (1 on, 4 standby); the picture list is `PICTURE_LIST` in `contracts/node_picture.py`. **Never
write D6 = 5** (hard off): a display may never come back from it remotely.

Test monitor support (spike, 2026-10-10):

| Action | CEC | DDC/CI | Signal off |
|---|---|---|---|
| Power off / on | no answer | yes (D6 4 / 1, read back), with a hot-plug blip on standby | built in 1b, not yet run live |
| Input switch | no | no: ignored, reads unstable, and the write blips | n/a |
| Brightness | n/a | yes (23 → 60 → 23, read back) | n/a |
| Contrast | n/a | yes as a register (0 → 50 → 0); effect on the picture not seen | n/a |

## What the controller did

```sh
sudo journalctl -b --no-pager -u photo-wall-display-controller.service
sudo journalctl -b --no-pager -u photo-wall-display.service | grep -i 'connector\|Output'
```

From Central, `GET /v1/operator/frames/{id}/power` (admin token) gives the methods that answered,
the method in use, the request in force with the Pi's seconds left, and the last result for the
change it names. Prefer the console's **Test: turn off** over hand writes for anything you want to
claim as the controller's behaviour.

## Picture: the display's own settings and Photo Wall's

Two groups, set apart ([Player architecture › Picture settings](../../../docs/player-architecture.md#picture-settings-1c)):
the display's own settings someone set in Photo Wall, which the controller writes over DDC/CI, and
Photo Wall's own (its four and the print look, [0023](../../../docs/decisions/0023-print-look.md)),
which the Player applies (light factor = Photo Wall brightness ÷ 100). Nothing
nobody set is ever written.

**Asking the picture list.** On a display's first appearance (per EDID identity, kept across
controller restarts) the controller asks every code on the picture list, a few codes per
`getvcp` (`ASK_BATCH`), each a short hold of the port at picture's turn, then reads the
`capabilities` string once for the names of list settings' choices. The same by hand:

```sh
B=13                                                        # from readlink .../card1-HDMI-A-1/ddc
D="sudo env XDG_CACHE_HOME=/tmp/ddc ddcutil --noconfig --bus $B"
$D getvcp 0C 10 12 14 --terse          # "VCP 10 C 60 100": a level, 60 of 100; "VCP 14 SNC x05": a list setting at value 5
$D getvcp 16 18 1A 87 --terse          # an "ERR" line or no line: not offered
$D capabilities                        # "Feature: 14 (Select color preset)" then "Values: 05: 6500 K …" names the choices
$D vcpinfo 14                          # ddcutil's name for a code (the console shows it)
```

A code that answers `C` is a level (0 to its maximum); one that answers `SNC` is a list setting,
editable only when `capabilities` names its values (at most 16), otherwise shown read-only. A
display that lists a code under `vcp(...)` may not answer it, and one that answers may not list
it (Traps), so the answers decide. The controller writes list settings before levels (a colour
preset before the gains it moves), reads before each write, writes only when the display reads
otherwise, and reads back; a choice the display does not list, or a level past its maximum, is
not written (`not-taken`). It reads the set and offered codes again from time to time and
reports their current values; a value changed on the display's buttons shows as "Changed on the
display's buttons", left alone until that setting is set again in Photo Wall. A change that moves
only Photo Wall's group runs no DDC/CI command.

**Power first.** Picture and power share the port's one DDC/CI channel. A waiting power command
always goes before a waiting picture command, a picture command holds the port at most
`PICTURE_HOLD_SECONDS` (a picture `setvcp` and `capabilities` are cut there too), and after a power
command the port waits `POWER_GRACE_SECONDS` for that operation's next command (`ddc.py`), so a
whole power operation waits at most one picture command. A batch, re-read or `capabilities` run
cut at the hold is logged by `picture_list.py` (`journalctl` on the display host unit: "did not
finish"); time a real batch, `setvcp` and `capabilities` on the bench before trusting 1.5 s.

From Central, `GET /v1/operator/frames/{id}/picture` (admin token) gives whether the display's
list is known, one row per hardware setting (offered with its range and current value, the value
set in Photo Wall, the last write's result with `before` and `after`), the Photo Wall values, and
whether the Player presented the latest change. In the database, `picture_reports.report` is the
Pi's latest picture report per port.

**Bench steps** (NOT RUN until a release with the picture split is on the test Pi; each needs the
Frame bound and its Position saved, or it shows no photos). Note the values to restore first:
`$D getvcp 10 12 --terse`.

1. **Which codes answer:** `$D getvcp` every code of `PICTURE_LIST` in batches of four, and
   `$D vcpinfo <code>` for each that answers; compare with the Display (hardware) section once it
   stops saying "Checking what this display offers", and record the codes and names.
2. **Ask timing:** `time $D getvcp 0C 10 12 14 --terse` (one batch) five times, and the whole ask
   from the controller's journal; record the spread.
3. **Both groups at once:** on the Picture tab set Display (hardware) Brightness to 46 and Photo
   Wall (software) Brightness to 114 %, wait for Done; `$D getvcp 10 --terse` reads 46, the picture
   route says `taken` with `before` the old value, the base page reads "Brightness: display 46 % ·
   Photo Wall 114 %", and the picture is visibly brighter than at 100 %.
4. **Power during an ask:** restart the controller (`sudo systemctl restart
   photo-wall-display-controller`) with a display whose list is not cached (another identity, or
   delete the controller's `offered-*.json`), and press **Test: turn off** at once: the result comes
   within its 5 s.
5. **Contrast:** set Display (hardware) Contrast to 25 and look at the screen; record whether the
   picture changes (the 1b spike saw the register change but no effect).
6. **Write timing:** `time $D setvcp 10 60` five times; record the spread. Some monitors save each
   write to memory that wears; the console sends at most 2 per second while dragging.
7. **Hot-plug after a write:** after step 3, `journalctl _PID=$(pgrep -x weston)` shows no
   `connector … disconnected`, and `photo-wall-node-player` is still running.
8. **Cover on the test monitor:** on the Position tab, Cover at 7 degrees and at 90 degrees: the
   picture is turned, never skewed, keeps the Frame profile's shape (still landscape at 90 degrees
   on a landscape Frame), covers the four corners, and nothing shows outside them.

Afterwards put the settings back from the console (Revert, or "As the display has it" and the old
value by hand), not only by hand: a hand write to a set code is reported as "Changed on the
display's buttons" and left alone.

## Signal range (Broadcast RGB)

Which numbers the Pi sends for black and white: the HDMI connector's DRM property "Broadcast RGB"
(`Automatic`, `Full`, `Limited 16:235`). Automatic sends Limited (black at 16) for every TV mode,
4K and 1080p included. Read it, root only (`debugfs`):

```sh
sudo sh -c 'grep -A11 "^connector\[.*HDMI-A" /sys/kernel/debug/dri/*/state' | grep -E 'connector|broadcast_rgb|is_limited_range|output_bpc'
sudo od -An -tx1 /sys/kernel/debug/dri/1/HDMI-A-1/infoframes/avi   # the AVI InfoFrame the TV gets
```

`broadcast_rgb` is the setting; `is_limited_range=y` is what goes out now; `output_bpc` the bits
per channel. In the AVI InfoFrame (header `82 02 0d`, then the checksum), the third payload byte's
bits 3–2 are the range the Pi labels: `0x04` limited, `0x08` full. The test Pi on v0.34.0, 4K60 on
HDMI-A-1, read `broadcast_rgb=Automatic`, `is_limited_range=y`, `output_bpc=8`, payload byte 3
`0x04`.

**Does the display honour the label?** The EDID's CTA Video Capability Data Block (extended tag
`0x00`, bytes `e2 00 <v>`): bit 6 of `<v>` (QS) set means RGB quantization is selectable, so the
display follows the AVI label; bit 7 (QY) is the same for YCC. The **Samsung QN43Q8F** sends
`e2 00 4f`: QS = 1, so it follows the Pi's label, and Full versus Limited is expected to look the
same on it (its glow is then the backlight; the Black level test pattern tells). Read the EDID
with `od -An -tx1 /sys/class/drm/card1-HDMI-A-1/edid` (no `edid-decode` on the image) and find
`e2 00`.

**Setting it.** Use the console (Picture tab › Signal range): the controller writes the request
file and the display unit's root pre-start step sets the connector before Weston starts, about
10 s without photos ([display host backend](../../../docs/display-host-backend.md#units-and-weston-incarnations)).
By hand it cannot be set while Weston runs: Weston holds the DRM master, and a property write from
another process is refused. The property survives a Weston restart (probed 2026-10-13) and goes
back to Automatic at a Pi start. Afterwards read `state` again; the controller reports what it
read back as the range in force.

## Reading the values while the owner tunes

Owner, 2026-10-13: "how can we improve the tuning flow so that you can read the values as we go?";
for matching a photo, "its easier for you (an LLM) to deal with bad picture quality". So when he
sends a phone photo of a Frame while tuning (the paper sheet, a test pattern), read what Central
had sent that Frame at that moment, and judge the photo yourself; no algorithm does it.

1. **Take the time on Central's clock.** As the photo arrives, read Central's time (the newest
   entry's `at`, or `SELECT now()`), never the phone's timestamp or your own clock; the photo was
   taken a little before it reached you. Every `at` is UTC.
2. **Read the snapshot:** `GET /v1/operator/frames/<frame-id>/tuning-snapshot?at=<UTC, e.g.
   2026-10-13T21:04:07Z>` with the admin token (curl and the paging routes in
   [Runbook › Configuration snapshots](../../../docs/runbook.md#configuration-snapshots)). It
   answers the Frame's Position entry (`fit`, `mat` with its lights) and its picture entry
   (`picture.hardware` by MCCS code, `picture.software`, `picture.signal_range`, the LUT and its
   recipe, `test_pattern`). For the minutes around it, read the Display's
   `…/picture-history` (newest first; `before=` pages back) and match the step he changed.
3. **What each kind means** (the table is [0025](../../../docs/decisions/0025-configuration-snapshots.md#decision)):
   `sent-draft` is on the wall but not kept (he has not pressed Done); `sent-back` is the wall put
   back after Revert or a closed tab; `sent-kept` and the `kept-*` kinds are what Photo Wall keeps.
   Entries of one session share `session_id`. `unrecorded_change: true` means the stored values
   changed outside Photo Wall since the newest kept entry.
4. **Allow for the Pi.** An entry is what Central sent, not what the Pi confirmed: the Pi shows it
   seconds later, about 10 s for a signal range change, so a photo taken right after a change may
   still show the step before. A pattern the Pi dropped at its own 10 minutes still shows in the
   entry.
5. **What is never seen:** the TV's own remote and menus (a Samsung's picture mode, its HDMI Black
   Level, Game Mode), the room's light, and the phone's exposure. A display (hardware) value
   changed on the display's buttons shows on the Picture tab ("Changed on the display's
   buttons"), not in the history.

**Without the operator token**, read the same with a read-only SQL session on Central's database
(SELECT only; never write): the two recipes in
[Runbook › Configuration snapshots](../../../docs/runbook.md#configuration-snapshots) give a Frame
at a time and a Display's last hour. Say which values you read, with their `at`, when you answer
him; to put old values back he presses Restore on the tab and then Done.

## Reading the calibration targets

Owner, 2026-10-13: "build the calibration targets"; the two stages are "the users choice -- not
something that the software enforces". He lays the printed paper target under the Frame's glass,
picks a target on the Picture tab and AirDrops a photo from his iPhone 14 Pro (ProRAW on): it
lands in `~/Downloads` on the Mac as `IMG_####.DNG`, not in the chat. He may photograph with the phone the
tab runs on: a test pattern stays up a minute while the tab is hidden (the picture session's idle
limit, `central/displays/picture.py` `idle_seconds`), and the tab keeps it once he is back.

1. **Run the reader** from the repository on the Mac (the admin token, and for a post-software
   reading with a LUT on a read-only database address, from 1Password; how, in
   [Runbook › Calibration targets](../../../docs/runbook.md#calibration-targets)):
   `op run -- .venv/bin/python -m scripts.calibration_target ~/Downloads/IMG_1234.DNG --central <url>`.
   Read the newest DNG he sent; one photo is enough (owner, 2026-10-14: "The point of using two
   calibration targets is that we are comparing them relatively.").
   It reads the QR, fetches the sent entry it names and prints JSON; exit 0 a reading, 2 a
   refusal, 1 an error.
2. **The stage is the entry's test pattern**, never your guess or the order he took them in:
   `target-pre-software` judges the display's own controls, `target-post-software` the residual for
   Photo Wall's values. Say which you read, with the entry's `at` and picture change.
3. **A refusal is a request to him**, with its `where` in plain words (the JSON's place words):
   - `target-not-found`: the markers are out of view, blurred or glared over: retake with the
     whole Frame in view and the named corner clear.
   - `code-unreadable`: the screen's QR did not read: retake straighter or closer, glare off it.
   - `unknown-entry`: Central has no such sent entry (another Central, or trimmed): pick the target
     again on the tab and retake.
   - `not-a-target`: the entry the QR names has no calibration target as its test pattern (the
     tab had moved on as the photo was taken): pick a target again and retake.
   - `clipped`: a white reached the sensor's clip: tap the screen on the phone and lower the
     exposure, or step back from a bright light.
   - `outside-photo`: part of a target (the named places) lies outside the photo, though enough
     markers were found: step back or centre the Frame in the photo.
   - `scale-mismatch`: the paper is not printed at 100 %: reprint without "fit to page".
   - `paper-misplaced`: the sheet, as laid, reaches over the screen target's patches (the detail
     says which way it is off): open the Frame and lay it centred and square in the dark box.
   - `white-unread`: too many groups dropped (reflections or shadows over them): move the light or
     lamp that reflects over the named places, or move himself, and retake.
   Dropped groups and copies in a reading (`reflection-edge`, `copy-unlike-the-rest`, …) are not
   refusals: mention where, if many, so he can move the light next time.
4. **Suggestions are his to make, never yours.** Pre-software: directions for the display's own
   controls (raise, lower, warmer, cooler or keep, with the gap's size); name the Samsung's menu
   item you think matches, and say it is your reading. Photo Wall does not record those menus: ask
   him what he changed. Post-software: values for Photo Wall's brightness, contrast, colour
   temperature and gamma, or "no change", which is the best result; the print look never moves.
   On a display that answers DDC/CI, the pre-software controls include the tab's Display
   (hardware) sliders; say which. `{"suggestion": {"why": "lut-unread"}}` means a post-software
   reading with a LUT on whose table could not be read: run again with
   `PHOTO_WALL_READ_DATABASE_URL` set; `tone-unread`, a post-software reading with no grey read (a
   normal photo, or every grey dropped): ask for a ProRAW photo. The paper's white is matched as
   read, with no correction for its ink (owner, 2026-10-14: "a level of nuance that probably
   doesn’t matter in practice").
   You never apply anything: no Picture edit, no Done, no DDC/CI write. He moves the sliders or
   the TV's controls, sends new photos, and presses Done when it reads right.
5. **Know the limits** ([0026](../../../docs/decisions/0026-calibration-targets.md#costs)): the
   camera is not the eye (ask him whether it looks matched); a shadow he casts over the paper
   alone (standing between the room light and the Frame) reads the screen too bright and is not
   detected, so ask him to stand aside of the light; a normal photo (JPEG, HEIC) gives the white alone; an angled photo reads the
   panel's white off. The pre-software black only says raise or keep (a crushed step), never
   lower: a black that glows, or a signal range the display does not expect, reads "keep". When he
   says the dark areas glow, read the entry's `signal_range` and use the Black level pattern
   ([0024](../../../docs/decisions/0024-display-tuning.md)) for it. The reader has not yet read a
   real ProRAW photo (NOT RUN until the first bench check): treat its first answers with care and
   look at the photo yourself.

## Traps

- **Some monitors drop the hot-plug signal on a DDC/CI standby or input write** (the test monitor,
  for about 250 ms). Weston tears its Output down and builds it again. Before 1b that killed the
  Player (SIGSEGV with descriptors exhausted) and nothing restarted it. After every write, check
  `journalctl _PID=<weston>` for `connector … disconnected` and that `photo-wall-node-player` is
  running.
- **The controller acts when a display comes back.** A hand `setvcp D6 4` that blips the signal
  makes the display reappear, and the controller then works it toward its document (standing on),
  so it may turn it back on. That is by design; test power through the console.
- **A hand test does not prove the controller can use DDC/CI.** Your `sudo ddcutil` works
  regardless of the unit's access. The controller needs the `i2c` group, which a release before
  the fix of [gap G15](../../../docs/player-architecture.md#observed-gaps) did not create; there
  `/dev/i2c-N` is closed to it, best detected falls to signal off and every brightness reads
  "no answer". Check `systemctl show photo-wall-display-controller -p SupplementaryGroups`.
- **Never switch the Pi's console (VT) to probe a range change.** The 2026-10-13 probe switched
  to tty2 so logind would pause Weston (the design's option A); systemd started `getty@tty2`, a
  login prompt, on the wall, and it stays running after you switch back. Check with
  `systemctl is-active getty@tty2`; stop it (`sudo systemctl stop getty@tty2`) or reboot the Pi.
  It was still active on the test Pi when 0024 was written.
- **Never send DDC/CI writes to a production Frame while it is showing.**
- **`ddcutil` under `sudo -E` writes a root-owned cache into your home directory.** Use
  `--noconfig` and `XDG_CACHE_HOME=/tmp/...`, as above.
- **No `kmod` on the image:** no `modprobe`, `lsmod`, `insmod` or `rmmod`. Read `/proc/modules` or
  `/sys/module/<name>`. The base carries no kernel modules of its own: stage 1 copies the initrd's
  module tree onto the new root, so a driver exists on a Pi only if the netboot initrd carried it,
  and `i2c-dev` loads only because `photo-wall-ddc.conf` names it (nothing loads it by alias).
- **External DRM DPMS is refused while Weston runs** (`modetest -M vc4 -w 35:DPMS:3` → Permission
  denied), and there is no `vcgencmd`. Signal off goes only through Weston's shell (op
  `output_power` on its control socket), which the display controller holds.
- **The `vcp()` capability list lies.** The test monitor advertises only `vcp(02)` yet answers
  10, 12 and D6. Probe codes directly; the controller asks the picture list for this reason and
  uses `capabilities` only for the names of a list setting's choices.
- **The bus number is the connector's, not a guess.** Take it from the `ddc` link every time.

## Spike tools without changing the image

For a Pi on a release without the tools or the driver:

```sh
mkdir -p /tmp/x/lists /tmp/x/cache /tmp/x/root && cd /tmp/x
apt-get -o Dir::State::Lists=/tmp/x/lists -o Dir::Cache=/tmp/x/cache -o Debug::NoLocking=1 update
apt-get -o Dir::State::Lists=/tmp/x/lists -o Dir::Cache=/tmp/x/cache -o Debug::NoLocking=1 download ddcutil i2c-tools libi2c0 v4l-utils libv4l2rds0t64 kmod
for deb in *.deb; do dpkg-deb -x "$deb" /tmp/x/root; done
export LD_LIBRARY_PATH=/tmp/x/root/usr/lib/aarch64-linux-gnu
```

The image's snapshot sources work for `apt-get download`; add any library a tool reports missing the same way. For `i2c-dev.ko`, take it from the exact
`linux-image-$(uname -r)` package at archive.raspberrypi.com, `dpkg-deb -x` it, and load it with
the extracted `insmod`. Afterwards: `cec-ctl --clear` on each device, `rmmod i2c_dev`, and
`rm -rf /tmp/x /tmp/ddc`.
