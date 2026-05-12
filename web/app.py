from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
from contextlib import suppress
from pathlib import Path

import asyncssh
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

logger = logging.getLogger("remote-vm-web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# --- Config ---------------------------------------------------------------

WEB_USER = os.environ["WEB_USER"]
WEB_PASSWORD = os.environ["WEB_PASSWORD"]

SSH_HOST = os.environ.get("SSH_HOST", "remote-vm")
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
SSH_USER = os.environ["SSH_USER"]
SSH_PASSWORD = os.environ["SSH_PASSWORD"]

SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)
COOKIE_NAME = "rvm_session"
COOKIE_MAX_AGE = int(os.environ.get("COOKIE_MAX_AGE_SECONDS", str(8 * 3600)))

BASE_DIR = Path(__file__).parent
serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="rvm-session-v1")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="remote-vm-gateway")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --- Auth helpers ---------------------------------------------------------


def _make_token() -> str:
    return serializer.dumps({"sub": WEB_USER})


def _check_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        data = serializer.loads(token, max_age=COOKIE_MAX_AGE)
    except BadSignature:
        return False
    return data.get("sub") == WEB_USER


def require_session(rvm_session: str | None = Cookie(default=None)) -> str:
    if not _check_token(rvm_session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return rvm_session


# --- HTTP routes ----------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request, rvm_session: str | None = Cookie(default=None)) -> Response:
    if not _check_token(rvm_session):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "terminal.html",
        {"user": WEB_USER, "ssh_user": SSH_USER, "ssh_host": SSH_HOST},
    )


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str | None = None) -> Response:
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
def login_submit(
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    user_ok = hmac.compare_digest(username, WEB_USER)
    pass_ok = hmac.compare_digest(password, WEB_PASSWORD)
    if not (user_ok and pass_ok):
        return RedirectResponse(url="/login?error=bad_credentials", status_code=303)

    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=COOKIE_NAME,
        value=_make_token(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        # Set secure=True only behind HTTPS. For local Docker we leave it off so the cookie works on http://localhost.
        secure=False,
        path="/",
    )
    return response


@app.post("/logout")
def logout() -> Response:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# --- WebSocket → SSH bridge ------------------------------------------------


@app.websocket("/ws")
async def ws_terminal(ws: WebSocket) -> None:
    token = ws.cookies.get(COOKIE_NAME)
    if not _check_token(token):
        await ws.close(code=4401)
        return

    await ws.accept()

    try:
        async with asyncssh.connect(
            host=SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            password=SSH_PASSWORD,
            known_hosts=None,
            keepalive_interval=15,
        ) as conn:
            term_size = await _read_size(ws, default=(80, 24))
            cols, rows = term_size

            process = await conn.create_process(
                term_type="xterm-256color",
                term_size=(cols, rows),
                stderr=asyncssh.STDOUT,
            )

            ws_to_ssh = asyncio.create_task(_pump_ws_to_ssh(ws, process))
            ssh_to_ws = asyncio.create_task(_pump_ssh_to_ws(process, ws))

            done, pending = await asyncio.wait(
                {ws_to_ssh, ssh_to_ws}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

            with suppress(Exception):
                process.terminate()
                await process.wait_closed()

    except (asyncssh.Error, OSError) as exc:
        logger.warning("SSH connection error: %s", exc)
        with suppress(Exception):
            await ws.send_text(f"\r\n\x1b[31m[gateway] SSH error: {exc}\x1b[0m\r\n")
    except WebSocketDisconnect:
        pass
    finally:
        with suppress(Exception):
            await ws.close()


async def _read_size(ws: WebSocket, default: tuple[int, int]) -> tuple[int, int]:
    """Read the first 'resize' message from the client; fall back to default."""
    try:
        first = await asyncio.wait_for(ws.receive_text(), timeout=2.0)
        msg = json.loads(first)
        if msg.get("type") == "resize":
            return int(msg["cols"]), int(msg["rows"])
        # Not a resize message — push it back as the first stdin write.
        if msg.get("type") == "stdin":
            return default
    except (asyncio.TimeoutError, ValueError, json.JSONDecodeError, KeyError):
        pass
    return default


async def _pump_ws_to_ssh(ws: WebSocket, process: asyncssh.SSHClientProcess) -> None:
    while True:
        text = await ws.receive_text()
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            continue
        kind = msg.get("type")
        if kind == "stdin":
            data = msg.get("data", "")
            if data:
                process.stdin.write(data)
        elif kind == "resize":
            cols, rows = int(msg.get("cols", 80)), int(msg.get("rows", 24))
            with suppress(Exception):
                process.change_terminal_size(cols, rows)


async def _pump_ssh_to_ws(process: asyncssh.SSHClientProcess, ws: WebSocket) -> None:
    while True:
        chunk = await process.stdout.read(4096)
        if not chunk:
            break
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        await ws.send_text(json.dumps({"type": "stdout", "data": chunk}))
