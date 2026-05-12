# remote-vm

A throwaway Ubuntu 24.04 VM running in Docker, with a browser terminal gated by an HTTP login form. SSH is not published to the host; the only normal entrypoint is the FastAPI/xterm.js gateway.

## Architecture

```text
[browser] -> http://127.0.0.1:7681 -> web (FastAPI + xterm.js) -> ssh:22 -> remote-vm (Ubuntu 24.04)
                                      signed cookie auth             internal docker network
```

- `remote-vm`: Ubuntu 24.04 + sshd, reachable only on the internal `vmnet` network.
- `web`: FastAPI gateway with signed-cookie login, WebSocket to `asyncssh`, and xterm.js.
- xterm.js assets are vendored into the image at build time, so runtime does not depend on a CDN.

## Run Locally

```sh
cp .env.example .env
docker compose up --build
```

Open http://127.0.0.1:7681 and sign in with `WEB_USER` / `WEB_PASSWORD`.

Try:

```sh
sudo apt update
sudo apt install -y jq python3
curl -I https://example.com
```

To wipe VM state and start clean:

```sh
docker compose down -v
```

## Fibe / Likeable

This repository is source-mount ready for Fibe:

- `web` exposes HTTP with `fibe.gg/expose: external:7681`.
- Both services point at `https://github.com/fibegg/remote-vm` and use `/workspace` as the Fibe source mount.
- The gateway runs with `uvicorn --reload`, so edits under `web/` hot-reload in Fibe source-mounted dev mode.
- `ports:` is only for local Docker; the Fibe pantry template omits it and routes through Traefik.

For iframe deployments, set:

```env
COOKIE_SECURE=true
COOKIE_SAMESITE=none
COOKIE_PARTITIONED=true
COOKIE_NAME=__Host-rvm_session
FRAME_ANCESTORS="http://localhost:* http://127.0.0.1:* http://$ROOT_DOMAIN http://*.$ROOT_DOMAIN https://$ROOT_DOMAIN https://*.$ROOT_DOMAIN"
CSP_MODE=enforce
```

Likeable can launch this as a non-default template by setting its `FIBE_TEMPLATE_VERSION_ID` to the imported Remote Vm template version. This does not require changing the global Fibe greenfield/Charge default.

## Images

The GitHub workflow builds:

- `ghcr.io/fibegg/remote-vm:dev-main` from `Dockerfile.vm`
- `ghcr.io/fibegg/remote-vm-web:dev-main` from `web/Dockerfile`

Local Compose builds the same Dockerfiles directly.

## Files

- `docker-compose.yml`: local services plus Fibe labels and template metadata.
- `Dockerfile.vm`: cacheable Ubuntu VM image.
- `web/Dockerfile`: FastAPI gateway image, built from the repo root.
- `web/app.py`: login, security headers, session cookies, and WebSocket to SSH bridge.
- `web/templates/`: login and terminal UI.
