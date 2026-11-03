# Playwright image ships Chromium and its system libraries; keep the tag in step with
# the playwright pin in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

# Ubuntu security fixes land between base image releases, and CI's Trivy gate
# fails on fixable HIGH and CRITICAL findings.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

# Playwright 1.64.x bundles Chromium 156.0.8078.4, a pre-stable build that lacks
# the fixes in Chrome 156 stable (2026-10-27), among them the critical
# CVE-2026-118306, -118331, -118344 and -118348. No 1.64 patch ships a newer one,
# so swap in Chrome for Testing stable over the bundled revision (1248), with
# pinned hashes. The fix is newer than the usual two-week wait. Drop this block
# once the Playwright pin moves past 1.64: the revision check fails the build to
# say so, rather than quietly putting an older Chromium over a newer one.
ARG CFT_VERSION=156.0.8078.80
RUN test -d /ms-playwright/chromium-1248 \
 || { echo "Base image no longer ships Chromium 1248: drop the Chrome for Testing override" >&2; exit 1; } \
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

COPY dealwatch ./dealwatch
COPY tests ./tests

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "dealwatch.app:app", "--host", "0.0.0.0", "--port", "8080"]
