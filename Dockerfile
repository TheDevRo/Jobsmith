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
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

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
COPY requirements.txt ./
RUN pip install -r requirements.txt

# X stack for the optional headed mode. fluxbox matters: Chromium under a
# bare Xvfb has no window manager, so dialogs can render off-screen.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify fluxbox \
    && rm -rf /var/lib/apt/lists/* \
    && test -f /usr/share/novnc/vnc.html

COPY backend ./backend
COPY frontend ./frontend
COPY extension ./extension
COPY tests ./tests
COPY config.example.yaml debug_apply.py linkedin_login.py pytest.ini ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# config.yaml, data/, resumes/, sessions/, failed_screenshots/, .browser-profile/
# come in via bind mounts at runtime. Pre-create so first boot has targets.
RUN mkdir -p data resumes sessions failed_screenshots .browser-profile

EXPOSE 8888 6080

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8888"]
