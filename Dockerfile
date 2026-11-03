# Playwright image ships Chromium and its system libraries; keep the tag in step with
# the playwright pin in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

# Ubuntu security fixes land between base image releases, and CI's Trivy gate
# fails on fixable HIGH and CRITICAL findings.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

ARG COMMIT=unknown
ENV COMMIT=$COMMIT \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt base-fixes.txt ./
# virtualenv, with python-discovery and filelock that only it needs, is build
# tooling from the base image that the app never uses; it carried most of
# Trivy's findings. pip check fails the build if anything else needed them.
#
# pip goes last: it vendors its own older certifi, msgpack and urllib3, and the
# running app never installs anything. /root/.cache holds virtualenv's seed
# wheels (an old setuptools).
RUN pip uninstall -y virtualenv python-discovery filelock \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir --upgrade -r base-fixes.txt \
 && pip check \
 && pip uninstall -y pip \
 && rm -rf /root/.cache

# Dependabot bumps the base image and the playwright pin in separate PRs, so for
# a while they can disagree. Install the browsers the pinned playwright expects
# (a no-op when the base image already has them), so either PR builds on its own.
RUN playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# Playwright 1.64.x uses Chromium revision 1248 (156.0.8078.4), a pre-stable
# build that lacks the fixes in Chrome 156 stable (2026-10-27), among them the
# critical CVE-2026-118306, -118331, -118344 and -118348. While the pinned
# playwright uses that revision, swap in Chrome for Testing stable, with pinned
# hashes. Any other revision is a newer Playwright and its own Chromium, and
# this step does nothing; delete it then.
ARG CFT_VERSION=156.0.8078.80
RUN rev=$(python -c "import json, pathlib, playwright; f = pathlib.Path(playwright.__file__).parent / 'driver/package/browsers.json'; print(next(b['revision'] for b in json.loads(f.read_text())['browsers'] if b['name'] == 'chromium'))") \
 && if [ "$rev" != 1248 ]; then echo "playwright uses Chromium $rev; Chrome for Testing override not needed"; exit 0; fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends unzip \
 && cd /tmp \
 && base=https://storage.googleapis.com/chrome-for-testing-public/$CFT_VERSION/linux64 \
 && curl -fsSLO $base/chrome-linux64.zip \
 && curl -fsSLO $base/chrome-headless-shell-linux64.zip \
 && printf '%s\n' \
      "b711c720f774be355197f32edf37e0651418ba036d92446ee2da19ac017540a7  chrome-linux64.zip" \
      "547dcd374e02961bf998f6046a267644f8502225c36bb78ca307dab32a0b3eb4  chrome-headless-shell-linux64.zip" \
    | sha256sum -c - \
 && rm -rf /ms-playwright/chromium-1248/chrome-linux64 \
           /ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-linux64 \
 && unzip -q chrome-linux64.zip -d /ms-playwright/chromium-1248 \
 && unzip -q chrome-headless-shell-linux64.zip -d /ms-playwright/chromium_headless_shell-1248 \
 && /ms-playwright/chromium-1248/chrome-linux64/chrome --version | grep -F "$CFT_VERSION" \
 && rm -f /tmp/*.zip \
 && apt-get purge -y unzip \
 && rm -rf /var/lib/apt/lists/*

# CI's tests and smoke run never start a browser, so check here that the one
# playwright will use launches and renders. A broken browser fails the build
# instead of shipping. Root, so no sandbox: that needs the stack's seccomp profile.
RUN python -c "from playwright.sync_api import sync_playwright as s; p = s().start(); [b.new_page().set_content('<p>ok</p>') or b.close() for b in (p.chromium.launch(channel='chromium'), p.chromium.launch())]; p.stop()"

COPY dealwatch ./dealwatch
COPY tests ./tests

# Run as the base image's unprivileged pwuser (uid 1001) so Chromium's sandbox can
# work. A bind-mounted /data must be writable by uid 1001. The XDG dirs point
# Chromium's profile and crash reports at /tmp, so the root filesystem can be
# mounted read-only.
RUN mkdir -p /data && chown pwuser:pwuser /data
ENV XDG_CONFIG_HOME=/tmp/.config \
    XDG_CACHE_HOME=/tmp/.cache
USER pwuser

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "dealwatch.app:app", "--host", "0.0.0.0", "--port", "8080"]
