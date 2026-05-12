FROM python:3.13-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# curl: to vendor xterm.js at build time so the runtime is offline-capable.
# tini-style PID 1 handled by uvicorn itself.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/web

COPY web/requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

# Vendor xterm.js + addon-fit at build time (no CDN dependency at runtime).
ARG XTERM_VERSION=5.5.0
ARG XTERM_FIT_VERSION=0.10.0
RUN mkdir -p static \
 && curl -fsSL "https://unpkg.com/@xterm/xterm@${XTERM_VERSION}/lib/xterm.js"            -o static/xterm.js \
 && curl -fsSL "https://unpkg.com/@xterm/xterm@${XTERM_VERSION}/css/xterm.css"           -o static/xterm.css \
 && curl -fsSL "https://unpkg.com/@xterm/addon-fit@${XTERM_FIT_VERSION}/lib/addon-fit.js" -o static/addon-fit.js

COPY web/app.py ./
COPY web/templates ./templates
COPY web/static/ ./static/

RUN groupadd --system --gid 1001 app \
 && useradd  --system --uid 1001 --gid 1001 --create-home --shell /bin/bash app \
 && chown -R app:app /workspace
USER app

EXPOSE 7681

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7681", "--proxy-headers", "--forwarded-allow-ips=*"]
