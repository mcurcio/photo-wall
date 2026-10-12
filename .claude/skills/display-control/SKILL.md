---
name: display-control
description: Operate a Photo Wall Pi's displays by hand over SSH - read a display's EDID, map HDMI ports to CEC devices and DDC/CI buses, turn a display off and on with cec-ctl or ddcutil, ask a display for the picture list (`getvcp <codes> --terse`, `capabilities`) and read and set its picture settings (brightness VCP 10, contrast VCP 12, colour preset 14, gains…), and read what the display controller did. Use it for the 1b bench step (Test off and on), the 1c and picture-split bench steps (display and Photo Wall brightness from the console read back, which codes a display answers, the time of an ask, power during an ask, Cover on the test monitor, write timing), to diagnose "Display didn't answer", "The display did not take it", "Checking what this display offers" that never clears or a power method that never answers, or to probe a new display's support. The rules (what the controller does, what Central decides) live in the docs it links; this is the runbook and its traps.
---

# display-control

How to reach and drive the displays on a Photo Wall Pi 5, learned in the 1b spike on the test Pi
(2026-10-10), the 1b build, the 1c build and the picture split ([0022](../../../docs/decisions/0022-picture-hardware-and-software-apart.md)). The rules are not here; they have one home each:

| Rules | Home |
|---|---|
| What the Pi's display controller does (when it acts, read before write, best detected, the Output document and report) | [Player architecture › Display identity and power](../../../docs/player-architecture.md#display-identity-and-power-1b) |
| Picture on the Pi (the two keys, asking the picture list, power first on the port, write order, newest wins, picture operations, frame protocol v4) | [Player architecture › Picture settings](../../../docs/player-architecture.md#picture-settings-1c) |
| How Central recognises a display, readiness, the Power and Picture tabs' words, Copy | [Operator console design §5–§7](../../../docs/operator-console-design.md#6-display-identity) |
| Central's side: tables, routes, the LISTEN wake, what each console result means | [Runbook › Display identity and power](../../../docs/runbook.md#display-identity-and-power) |
| SSH access to a Node | [Runbook › Reaching a Node over SSH](../../../docs/runbook.md#reaching-a-node-over-ssh) |
| The code | `appliance/display_host/ddc.py` (the one place `ddcutil` runs), `power_methods.py` (the exact power tool calls), `picture_list.py` (the ask), `picture.py` (writes and the base page line), `output_power.py`, `edid.py`; `contracts/node_output.py`, `contracts/node_picture.py` (the picture list and its names) |

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
