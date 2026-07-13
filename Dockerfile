# Jobsmith container image.
#
# Base image ships Chromium + every system lib Playwright needs, which is
# the bulk of what's annoying to assemble by hand.
#
# Two run modes, toggled by BROWSER_HEADLESS:
#   true  (default) — headless server; interactive site logins are unavailable
#                     (use the cookie importer in Settings → Integrations).
#   false           — the entrypoint starts Xvfb + x11vnc + noVNC so headed
#                     Chromium windows (LinkedIn/Indeed logins, live apply
#                     runs) are visible at http://<host>:6080/vnc.html.
#
# PLAYWRIGHT_VERSION must match the `playwright==` pin in requirements.lock /
# requirements.txt — the base image bundles exactly the browser build that
# Playwright version expects. The RUN check below enforces it at build time.
ARG PLAYWRIGHT_VERSION=1.58.0
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble

# ARGs don't survive FROM — re-declare for the check below.
ARG PLAYWRIGHT_VERSION

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    BROWSER_HEADLESS=true \
    JOBSMITH_IN_DOCKER=1 \
    DISPLAY=:99

WORKDIR /app

# pip deps must install BEFORE the apt X stack: websockify pulls in
# Debian-managed Python libs (python3-typing-extensions et al.) that pip
# cannot upgrade past ("no RECORD file"), breaking the install.
#
# Install from the lock (fully pinned) rather than requirements.txt so image
# builds are reproducible. requirements-optional.txt (browser-use) is
# deliberately NOT installed — it's feature-flagged and lazily imported.
COPY requirements.txt requirements.lock ./
RUN grep -qx "playwright==${PLAYWRIGHT_VERSION}" requirements.lock \
    || { echo "ERROR: base image playwright v${PLAYWRIGHT_VERSION} does not match the playwright pin in requirements.lock"; exit 1; }
RUN pip install -r requirements.lock

# X stack for the optional headed mode. fluxbox matters: Chromium under a
# bare Xvfb has no window manager, so dialogs can render off-screen.
# rsync + zip: needed by extension/scripts/build.sh below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify fluxbox rsync zip \
    && rm -rf /var/lib/apt/lists/* \
    && test -f /usr/share/novnc/vnc.html

COPY backend ./backend
COPY frontend ./frontend
COPY extension ./extension

# Build extension/dist (gitignored, so never in the build context): the
# install/download endpoints serve these zips, and the committed signed
# Firefox XPI in extension/signed/ gets staged into the artifacts dir.
RUN bash extension/scripts/build.sh

# Runtime image ships no tests and no debug scripts (tests/, debug_apply.py,
# linkedin_login.py, pytest.ini are dev-only — see .dockerignore).
COPY config.example.yaml ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# config.yaml, data/, resumes/, sessions/, failed_screenshots/, .browser-profile/
# come in via bind mounts at runtime. Pre-create so first boot has targets.
RUN mkdir -p data resumes sessions failed_screenshots .browser-profile

# Don't run the backend, Chromium, or the X stack as root: they process
# untrusted web content, and a root container writes root-owned files into the
# bind-mounted ./data and ./resumes on the host. The Playwright base image
# already ships a non-root `pwuser` (uid 1000) with a home dir.
#
# Bind-mount note: host-side ./data, ./resumes, ./config, … must be writable by
# uid 1000. Docker Desktop (macOS/Windows) maps this automatically; on Linux the
# first non-system user is usually uid 1000 already, otherwise
# `sudo chown -R 1000:1000 data resumes config sessions failed_screenshots .browser-profile`.
RUN chown -R pwuser:pwuser /app
USER pwuser

EXPOSE 8888 6080

# `restart: unless-stopped` can't tell a hung uvicorn from a healthy one, so
# probe the AI-free liveness endpoint (/api/health does an LM Studio round-trip
# and would fail the container whenever the model server is down).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8888/api/health/live',timeout=4).status==200 else 1)"

# "serve" makes the entrypoint launch uvicorn itself, resolving the bind
# interface from JOBSMITH_HOST / the mounted config's server.host.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["serve"]
