# remote-vm

A throwaway Ubuntu 24.04 VM running in Docker, with a browser-based terminal gated by an HTTP login form. SSH is **not** published to the host — the only way in is through the auth gateway.

## Architecture

```
[browser]  →  http://127.0.0.1:7681  →  web (FastAPI + xterm.js)  →  ssh:22  →  remote-vm (Ubuntu 24.04)
                                       (signed cookie auth)         (internal docker network)
```

- `remote-vm` — Ubuntu 24.04 + sshd, port 22 only on the internal `vmnet` network
- `web` — FastAPI gateway: login page, signed-cookie session, WebSocket → `asyncssh` → PTY → xterm.js in the browser
- xterm.js + addon-fit are vendored at image build time (no CDN at runtime)

## Run

```sh
cp .env.example .env   # edit WEB_USER/WEB_PASSWORD/SESSION_SECRET
docker compose up --build
```

Open http://127.0.0.1:7681 → sign in with `WEB_USER` / `WEB_PASSWORD`. You get a full PTY into the VM.

Try:

```sh
sudo apt update
sudo apt install -y jq python3
curl -I https://example.com
```

## Adding pre-installed packages

Edit the apt-get list inside `dockerfile_inline` (the `remote-vm` service in `docker-compose.yml`), then:

```sh
docker compose up --build
```

The change rebuilds the image. The VM filesystem (`/etc`, `/usr`, `/var`, `/opt`, `/root`, `/home`) is persisted in named volumes — to wipe state and start clean:

```sh
docker compose down -v
```

## Security model

- The VM's port 22 is NEVER published to the host (`expose: ["22"]`, no `ports`). Only `web` can reach it across the docker network.
- `web` is bound to `127.0.0.1:7681` only — not reachable from the LAN. Put a TLS-terminating reverse proxy (Caddy, nginx) in front for remote access.
- Login uses constant-time comparison (`hmac.compare_digest`).
- Session cookie is HTTP-only, signed via `itsdangerous` with `SESSION_SECRET`.
- `secure=True` on the cookie is intentionally OFF for `http://localhost`. **Turn it on once you put HTTPS in front** — see `web/app.py`.
- This is a single-user gateway (one `WEB_USER`/`WEB_PASSWORD`). For multi-user, swap the credential check for a real user store.

## Files of note

- `docker-compose.yml` — both services + sshd config inline
- `web/app.py` — gateway, login, WebSocket → SSH PTY bridge
- `web/templates/{login,terminal}.html` — UI
- `web/Dockerfile` — image build, vendors xterm.js
