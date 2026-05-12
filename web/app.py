from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
from contextlib import suppress
from pathlib import Path
from typing import Iterable

import asyncssh
from fastapi import (
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
COOKIE_NAME = os.environ.get("COOKIE_NAME", "rvm_session")
COOKIE_MAX_AGE = int(os.environ.get("COOKIE_MAX_AGE_SECONDS", str(8 * 3600)))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "auto").strip().lower()
COOKIE_SAMESITE = os.environ.get("COOKIE_SAMESITE", "lax").strip().lower()
COOKIE_PARTITIONED = os.environ.get("COOKIE_PARTITIONED", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CSP_MODE = os.environ.get("CSP_MODE", "enforce").strip().lower()
FRAME_ANCESTORS = os.environ.get("FRAME_ANCESTORS", "self")
STATIC_CACHE_SECONDS = int(os.environ.get("STATIC_CACHE_SECONDS", "3600"))

BASE_DIR = Path(__file__).parent
serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="rvm-session-v1")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="remote-vm-gateway")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --- Response policy -------------------------------------------------------


@app.middleware("http")
async def response_policy_headers(request: Request, call_next) -> Response:
    response = await call_next(request)

    if request.url.path.startswith("/static/"):
        response.headers.setdefault(
            "Cache-Control",
            f"public, max-age={STATIC_CACHE_SECONDS}, must-revalidate",
        )
    else:
        response.headers.setdefault("Cache-Control", "no-store")

    csp = _content_security_policy()
    if csp and CSP_MODE != "off":
        header = (
            "Content-Security-Policy-Report-Only"
            if CSP_MODE in {"report", "report-only"}
            else "Content-Security-Policy"
        )
        response.headers.setdefault(header, csp)

    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=()",
    )
    return response


def _content_security_policy() -> str:
    frame_ancestors = " ".join(_normalize_csp_sources(FRAME_ANCESTORS.split()))
    if not frame_ancestors:
        frame_ancestors = "'self'"

    directives = [
        "default-src 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "img-src 'self' data:",
        "font-src 'self' data:",
        "style-src 'self' 'unsafe-inline'",
        "script-src 'self' 'unsafe-inline'",
        "connect-src 'self' ws: wss:",
        f"frame-ancestors {frame_ancestors}",
    ]
    return "; ".join(directives)


def _normalize_csp_sources(values: Iterable[str]) -> list[str]:
    result = []
    for value in values:
        source = value.strip()
        if not source:
            continue
        lowered = source.lower().strip("'")
        if lowered in {"self", "none"}:
            result.append(f"'{lowered}'")
        else:
            result.append(source)
    return result


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


def require_session(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME)
    if not _check_token(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return token


def _request_is_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    if forwarded_proto:
        return forwarded_proto == "https"
    forwarded_ssl = request.headers.get("x-forwarded-ssl", "").strip().lower()
    if forwarded_ssl:
        return forwarded_ssl == "on"
    return request.url.scheme == "https"


def _cookie_secure(request: Request) -> bool:
    if COOKIE_SECURE in {"1", "true", "yes", "on"}:
        return True
    if COOKIE_SECURE in {"0", "false", "no", "off"}:
        return False
    return _request_is_https(request)


def _cookie_samesite() -> str:
    if COOKIE_SAMESITE in {"strict", "lax", "none"}:
        return {"strict": "Strict", "lax": "Lax", "none": "None"}[COOKIE_SAMESITE]
    logger.warning("Invalid COOKIE_SAMESITE=%r, falling back to Lax", COOKIE_SAMESITE)
    return "Lax"


def _append_session_cookie(response: Response, request: Request, value: str, max_age: int) -> None:
    secure = _cookie_secure(request)
    samesite = _cookie_samesite()
    if COOKIE_PARTITIONED and samesite != "None":
        samesite = "None"
    if samesite == "None" and not secure:
        logger.warning("SameSite=None cookies require Secure; current request is not HTTPS")

    parts = [
        f"{COOKIE_NAME}={value}",
        "Path=/",
        f"Max-Age={max_age}",
        "HttpOnly",
        f"SameSite={samesite}",
    ]
    if max_age <= 0:
        parts.append("Expires=Thu, 01 Jan 1970 00:00:00 GMT")
    if secure:
        parts.append("Secure")
    if COOKIE_PARTITIONED:
        parts.append("Partitioned")
    response.headers.append("Set-Cookie", "; ".join(parts))


def _set_session_cookie(response: Response, request: Request) -> None:
    _append_session_cookie(response, request, _make_token(), COOKIE_MAX_AGE)


def _delete_session_cookie(response: Response, request: Request) -> None:
    _append_session_cookie(response, request, "", 0)


# --- HTTP routes ----------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> Response:
    if not _check_token(request.cookies.get(COOKIE_NAME)):
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
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    user_ok = hmac.compare_digest(username, WEB_USER)
    pass_ok = hmac.compare_digest(password, WEB_PASSWORD)
    if not (user_ok and pass_ok):
        return RedirectResponse(url="/login?error=bad_credentials", status_code=303)

    response = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(response, request)
    return response


@app.post("/logout")
def logout(request: Request) -> Response:
    response = RedirectResponse(url="/login", status_code=303)
    _delete_session_cookie(response, request)
    return response


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# --- WebSocket to SSH bridge ---------------------------------------------


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
            term_size, pending_stdin = await _read_size(ws, default=(80, 24))
            cols, rows = term_size

            process = await conn.create_process(
                term_type="xterm-256color",
                term_size=(cols, rows),
                stderr=asyncssh.STDOUT,
            )

            ws_to_ssh = asyncio.create_task(_pump_ws_to_ssh(ws, process, pending_stdin))
            ssh_to_ws = asyncio.create_task(_pump_ssh_to_ws(process, ws))

            _done, pending = await asyncio.wait(
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


async def _read_size(ws: WebSocket, default: tuple[int, int]) -> tuple[tuple[int, int], list[str]]:
    """Read the first resize message; preserve early stdin if the client sent data first."""
    try:
        first = await asyncio.wait_for(ws.receive_text(), timeout=2.0)
        msg = json.loads(first)
        if msg.get("type") == "resize":
            return (int(msg["cols"]), int(msg["rows"])), []
        if msg.get("type") == "stdin":
            return default, [str(msg.get("data", ""))]
    except (asyncio.TimeoutError, ValueError, json.JSONDecodeError, KeyError):
        pass
    return default, []


async def _pump_ws_to_ssh(
    ws: WebSocket,
    process: asyncssh.SSHClientProcess,
    pending_stdin: list[str] | None = None,
) -> None:
    for data in pending_stdin or []:
        if data:
            process.stdin.write(data)

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
