#!/bin/sh
# Stages the appliance bootstrapper's minimal Python module closure and its
# systemd unit into layer/photo-wall-bootstrapper.rootfs-overlay/, copied
# from the real repo sources -- deliberately NOT hand-duplicated/committed
# into this tree, so the overlay can never drift from appliance/provision.py,
# player/mdns_discovery.py, etc. Run by .github/workflows/base-image.yml
# immediately before invoking rpi-image-gen; safe to re-run (wipes and
# re-stages the overlay directory each time).
#
# The import closure staged here is exactly what appliance/provision.py
# imports at runtime (see its module docstring, "Reuse and the import-
# boundary decision"): stdlib only, plus `player.mdns_discovery
# .MdnsCentralDiscovery` (never `player.service`, which pulls in GTK/
# GStreamer). player/discovery.py is included too, per the task brief,
# as the Protocol `Bootstrapper.discovery` is documented to match
# (appliance/provision.py's class docstring) even though provision.py does
# not import it directly. No `contracts` module is imported by
# appliance/provision.py in this slice (see .claude/errata.md,
# p3-base-bootstrapper: the boot-context/contracts.equipment production is
# explicitly out of scope for this bootstrapper), so none is staged here.
#
# Usage: stage_overlay.sh <repo-root>

set -eu

repo=$1
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
overlay="$here/layer/photo-wall-bootstrapper.rootfs-overlay"

rm -rf "$overlay"
install -d -m 0755 \
  "$overlay/usr/lib/python3/dist-packages/appliance" \
  "$overlay/usr/lib/python3/dist-packages/player" \
  "$overlay/etc/systemd/system"

install -m 0644 \
  "$repo/appliance/__init__.py" \
  "$repo/appliance/provision.py" \
  "$overlay/usr/lib/python3/dist-packages/appliance/"

install -m 0644 \
  "$repo/player/__init__.py" \
  "$repo/player/mdns_discovery.py" \
  "$repo/player/discovery.py" \
  "$overlay/usr/lib/python3/dist-packages/player/"

install -m 0644 \
  "$repo/appliance/systemd/photo-wall-provision.service" \
  "$overlay/etc/systemd/system/photo-wall-provision.service"

echo "stage_overlay: staged $(find "$overlay" -type f | wc -l) files under $overlay"
