#!/bin/sh
# Run inside the disposable native image; no host display/device access.
set -eu
export XDG_RUNTIME_DIR=/tmp/photo-wall-wayland
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
if [ "${PHOTO_WALL_NATIVE_MULTI:-0}" = 1 ]; then
  cat > /tmp/photo-wall-weston.ini <<'EOF'
[core]
shell=kiosk-shell.so
idle-time=0
[output]
name=X1
mode=640x480
app-ids=org.photowall.hdmi1
[output]
name=X2
mode=640x480
app-ids=org.photowall.hdmi2
EOF
  weston --backend=x11-backend.so --output-count=2 --width=640 --height=480 \
    --socket=photo-wall-wayland --config=/tmp/photo-wall-weston.ini \
    --log=/tmp/photo-wall-weston.log &
else
  weston --backend=headless-backend.so --use-gl --width=640 --height=480 \
    --shell=kiosk-shell.so --socket=photo-wall-wayland --idle-time=0 --no-config \
    --log=/tmp/photo-wall-weston.log &
fi
compositor=$!
trap 'kill "$compositor" 2>/dev/null || true' EXIT INT TERM
attempt=0
while [ ! -S "$XDG_RUNTIME_DIR/photo-wall-wayland" ]; do
  attempt=$((attempt+1))
  if [ "$attempt" -ge 100 ] || ! kill -0 "$compositor" 2>/dev/null; then
    cat /tmp/photo-wall-weston.log
    exit 1
  fi
  sleep .1
done
export WAYLAND_DISPLAY=photo-wall-wayland GDK_BACKEND=wayland
if ! WAYLAND_DEBUG=client /opt/native-venv/bin/python tests/native/smoke.py 2>/tmp/photo-wall-wayland-client.log; then
  cat /tmp/photo-wall-wayland-client.log
  exit 1
fi
if [ "${PHOTO_WALL_NATIVE_MULTI:-0}" = 1 ]; then
  /opt/native-venv/bin/python tests/native/check_wayland.py /tmp/photo-wall-wayland-client.log --two-outputs
else
  /opt/native-venv/bin/python tests/native/check_wayland.py /tmp/photo-wall-wayland-client.log
fi
