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

COPY dealwatch ./dealwatch
COPY tests ./tests

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "dealwatch.app:app", "--host", "0.0.0.0", "--port", "8080"]
