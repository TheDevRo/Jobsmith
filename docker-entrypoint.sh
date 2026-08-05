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

# On Linux, a bind-mount directory the engine had to create itself is owned by
# root, while we run as pwuser (uid 1000) — every write below then fails with a
# bare "Permission denied" and `set -e` turns it into a crash loop. Say what to
# do instead.
if [ ! -w /app/config ]; then
    echo "[entrypoint] ==============================================================="
    echo "[entrypoint] /app/config is not writable by this container's user (uid $(id -u))."
    echo "[entrypoint] On Linux, Docker creates missing bind-mount directories as root."
    echo "[entrypoint] Fix from the directory holding docker-compose.yml:"
    echo "[entrypoint]     sudo chown -R 1000:1000 config data resumes sessions \\"
    echo "[entrypoint]         failed_screenshots .browser-profile sync-folder"
    echo "[entrypoint] (Creating them yourself before the first 'docker compose up'"
    echo "[entrypoint]  avoids this entirely — see the Docker quickstart in README.)"
    echo "[entrypoint] ==============================================================="
    exit 1
fi

if [ ! -e /app/config/config.yaml ]; then
    echo "[entrypoint] No config/config.yaml found, seeding from config.example.yaml."
    cp /app/config.example.yaml /app/config/config.yaml
    # It will hold API keys and ATS passwords the moment the user saves Settings.
    chmod 600 /app/config/config.yaml
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

    # A VNC session on this container is a live remote desktop into a browser
    # that is logged into LinkedIn/Indeed. Password-less x11vnc is only
    # acceptable while the published 6080 port is loopback-only. NOVNC_BIND is
    # passed in by docker-compose and mirrors the host-side port binding; if it
    # is anything other than loopback and no VNC_PASSWORD was set, generate one
    # rather than serving an open desktop to the LAN.
    case "${NOVNC_BIND:-127.0.0.1}" in
        ""|127.0.0.1|localhost|::1|"[::1]") NOVNC_LOOPBACK=1 ;;
        *)                                  NOVNC_LOOPBACK=0 ;;
    esac

    if [ "$NOVNC_LOOPBACK" = "0" ] && [ -z "$VNC_PASSWORD" ]; then
        VNC_PASSWORD=$(python -c 'import secrets; print(secrets.token_urlsafe(12))')
        echo "[entrypoint] ==============================================================="
        echo "[entrypoint] NOVNC_BIND=${NOVNC_BIND} exposes noVNC beyond loopback but"
        echo "[entrypoint] VNC_PASSWORD is empty. Refusing to run x11vnc with -nopw."
        echo "[entrypoint] Generated a one-time VNC password (shown only now):"
        echo "[entrypoint]     ${VNC_PASSWORD}"
        echo "[entrypoint] Set VNC_PASSWORD in .env to pick your own and keep it stable."
        echo "[entrypoint] ==============================================================="
    fi

    Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &

    # Wait until the display actually accepts clients instead of a blind
    # sleep — on a slow container fluxbox/x11vnc would otherwise race Xvfb
    # and die with "cannot open display" while the entrypoint sails on.
    XVFB_READY=0
    for _ in $(seq 1 50); do
        if [ -S /tmp/.X11-unix/X99 ]; then XVFB_READY=1; break; fi
        sleep 0.2
    done
    if [ "$XVFB_READY" = "0" ]; then
        echo "[entrypoint] WARNING: Xvfb did not create display :99 within 10s —"
        echo "[entrypoint] headed browsing and noVNC will not work this run."
    fi

    fluxbox >/dev/null 2>&1 &

    # x11vnc and websockify are supervised: each runs in a restart loop so a
    # crash (x11vnc is known to fall over on long-lived containers) means a
    # 2-second gap in http://localhost:6080/vnc.html, not a permanently blank
    # screen with nothing in `docker logs`. Restarts are logged to stdout;
    # the processes' own output goes to /tmp/*.log inside the container.
    if [ -n "$VNC_PASSWORD" ]; then
        ( set +e; while :; do
            x11vnc -display :99 -forever -shared -nobg -passwd "$VNC_PASSWORD" >>/tmp/x11vnc.log 2>&1
            echo "[entrypoint] x11vnc exited (status $?) — restarting in 2s (log: /tmp/x11vnc.log)"
            sleep 2
        done ) &
    else
        # Loopback-only publish: no password, matching the desktop-app UX.
        ( set +e; while :; do
            x11vnc -display :99 -forever -shared -nobg -nopw >>/tmp/x11vnc.log 2>&1
            echo "[entrypoint] x11vnc exited (status $?) — restarting in 2s (log: /tmp/x11vnc.log)"
            sleep 2
        done ) &
    fi

    ( set +e; while :; do
        websockify --web /usr/share/novnc 6080 localhost:5900 >>/tmp/websockify.log 2>&1
        echo "[entrypoint] websockify exited (status $?) — restarting in 2s (log: /tmp/websockify.log)"
        sleep 2
    done ) &
fi

# ── Server launch ─────────────────────────────────────────────────────────────
# The default CMD is "serve": launch uvicorn here so the bind interface can
# come from JOBSMITH_HOST or the mounted config's server.host (set from
# Settings → Integrations → Network). Loopback values are treated as unset —
# binding 127.0.0.1 inside the container would make the published port dead
# (and freshly seeded configs default to 127.0.0.1); to restrict access to
# the docker host, bind the port mapping instead, e.g. "127.0.0.1:8888:8888".
if [ "$1" = "serve" ]; then
    HOST="${JOBSMITH_HOST:-}"
    if [ -z "$HOST" ]; then
        HOST=$(python -c 'import yaml; cfg = yaml.safe_load(open("/app/config/config.yaml")) or {}; print((cfg.get("server") or {}).get("host") or "")' 2>/dev/null || true)
    fi
    case "$HOST" in ""|127.0.0.1|localhost|::1)
        [ -n "$HOST" ] && echo "[entrypoint] server.host=$HOST is loopback — binding 0.0.0.0 (use a port mapping like 127.0.0.1:8888:8888 to restrict access)."
        HOST=0.0.0.0
        ;;
    esac
    PORT="${JOBSMITH_PORT:-8888}"
    echo "[entrypoint] Starting backend on $HOST:$PORT"
    exec python -m uvicorn backend.main:app --host "$HOST" --port "$PORT"
fi

exec "$@"
