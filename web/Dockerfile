FROM python:3.13-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/web

COPY web/requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY web/app.py ./
COPY web/templates ./templates
COPY web/static/ ./static/

RUN groupadd --system --gid 1001 app \
 && useradd  --system --uid 1001 --gid 1001 --create-home --shell /bin/bash app \
 && chown -R app:app /workspace
USER app

EXPOSE 7681

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7681", "--proxy-headers", "--forwarded-allow-ips=*"]
