# Application integration fixture only; this is not the Pi appliance image.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 python3.12-venv python3-gi python3-gst-1.0 python3-opengl \
    gir1.2-gtk-3.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav \
    libgl1-mesa-dri libegl1-mesa-dev xvfb xauth ffmpeg weston ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN python3.12 -m venv --system-site-packages /opt/native-venv \
    && /opt/native-venv/bin/pip install --no-cache-dir pydantic==2.11.4 Pillow==12.3.0
WORKDIR /app
COPY contracts /app/contracts
COPY player /app/player
COPY tests/native /app/tests/native
ENV PYTHONPATH=/app LIBGL_ALWAYS_SOFTWARE=1 PYOPENGL_PLATFORM=egl GDK_GL=gles
CMD ["xvfb-run", "-a", "-s", "-screen 0 640x480x24", "/opt/native-venv/bin/python", "tests/native/smoke.py"]
