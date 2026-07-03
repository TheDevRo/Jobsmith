#!/bin/sh
set -e

# We bind-mount ./config:/app/config (a directory) instead of bind-mounting
# the config.yaml file directly. The file approach trips a classic Docker
# gotcha: when the host file doesn't exist, Docker silently creates it as an
# empty *directory*, which then can't be opened as a file and can't be
# rmdir'd from inside the container (it's a live bind mount).
#
# Directory bind mounts don't have that problem — Docker creates the host
# dir cleanly. We seed config/config.yaml from the example on first run and
# symlink it into /app/config.yaml so the backend (which reads /app/config.yaml)
# is none the wiser.

mkdir -p /app/config

if [ ! -e /app/config/config.yaml ]; then
    echo "[entrypoint] No config/config.yaml found, seeding from config.example.yaml."
    cp /app/config.example.yaml /app/config/config.yaml
fi

# Re-link every boot so the symlink is always current even if the user
# swapped the file out.
ln -sf /app/config/config.yaml /app/config.yaml

# ── Optional headed mode: Xvfb + x11vnc + noVNC ──────────────────────────────
# When BROWSER_HEADLESS=false, headed Chromium needs a display. Xvfb provides
# a virtual one at :99 (matching ENV DISPLAY in the Dockerfile), x11vnc serves
# it over VNC, and websockify/noVNC makes it reachable from a plain browser at
# http://<host>:6080/vnc.html. This is how interactive LinkedIn/Indeed logins
# work inside the container.
if [ "$BROWSER_HEADLESS" = "false" ]; then
    echo "[entrypoint] Headed mode: starting Xvfb + x11vnc + noVNC on :6080."
    Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &

    # Give the display a moment before clients attach.
    sleep 1
    fluxbox >/dev/null 2>&1 &

    if [ -n "$VNC_PASSWORD" ]; then
        x11vnc -display :99 -forever -shared -bg -passwd "$VNC_PASSWORD" -o /tmp/x11vnc.log
    else
        x11vnc -display :99 -forever -shared -bg -nopw -o /tmp/x11vnc.log
    fi

    websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/websockify.log 2>&1 &
fi

exec "$@"
