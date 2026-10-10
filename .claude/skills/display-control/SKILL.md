---
name: display-control
description: Operate a Photo Wall Pi's displays by hand over SSH - read a display's EDID, map HDMI ports to CEC devices and DDC/CI buses, turn a display off and on with cec-ctl or ddcutil, and read what the display controller did. Use it for the 1b bench step (Test off and on), to diagnose "Display didn't answer" or a power method that never answers, or to probe a new display's support. The rules (what the controller does, what Central decides) live in the docs it links; this is the runbook and its traps.
---

# display-control

How to reach and drive the displays on a Photo Wall Pi 5, learned in the 1b spike on the test Pi
(2026-10-10) and the 1b build. The rules are not here; they have one home each:

| Rules | Home |
|---|---|
| What the Pi's display controller does (when it acts, read before write, best detected, the Output document and report) | [Player architecture › Display identity and power](../../../docs/player-architecture.md#display-identity-and-power-1b) |
| How Central recognises a display, readiness, the Power tab's words | [Operator console design §6–§7](../../../docs/operator-console-design.md#6-display-identity) |
| Central's side: tables, routes, the LISTEN wake, what each console result means | [Runbook › Display identity and power](../../../docs/runbook.md#display-identity-and-power) |
| SSH access to a Node | [Runbook › Reaching a Node over SSH](../../../docs/runbook.md#reaching-a-node-over-ssh) |
| The code | `appliance/display_host/power_methods.py` (the exact tool calls), `output_power.py`, `edid.py`; `contracts/node_output.py` |

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

VCP codes: `10` brightness, `12` contrast, `60` input, `D6` power (1 on, 4 standby). **Never
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

## Traps

- **Some monitors drop the hot-plug signal on a DDC/CI standby or input write** (the test monitor,
  for about 250 ms). Weston tears its Output down and builds it again. Before 1b that killed the
  Player (SIGSEGV with descriptors exhausted) and nothing restarted it. After every write, check
  `journalctl _PID=<weston>` for `connector … disconnected` and that `photo-wall-node-player` is
  running.
- **The controller acts when a display comes back.** A hand `setvcp D6 4` that blips the signal
  makes the display reappear, and the controller then works it toward its document (standing on),
  so it may turn it back on. That is by design; test power through the console.
- **The controller cannot use DDC/CI yet.** Its unit is not in the `i2c` group (the node package
  does not create the group), so `/dev/i2c-N` is closed to it and best detected falls to signal
  off on a DDC/CI-only monitor ([gap G15](../../../docs/player-architecture.md#observed-gaps)). Your
  `sudo ddcutil` works regardless, so a hand test proving DDC/CI does not prove the controller can.
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
  10, 12 and D6. Probe codes directly.
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
