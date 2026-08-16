"""API: auth router — register / login / logout (RBAC roles, signed sessions).

Sessions are stored server-side (``session`` table) and referenced by a signed,
tamper-proof cookie (``kb_session``). Passwords are PBKDF2-hashed (security.py).
Human members register with a username + password and a role (admin/member);
agents are registered separately (slice C) and have kind=agent / role=agent.
"""
from __future__ import annotations

import uuid
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...config import Settings, get_settings
from ...security import new_session_id, sign_session, unsign_session
from ...domain.ports import UnitOfWork
from ..deps import SESSION_COOKIE, CurrentUserDep, UowDep
from ..rendering import navigation_context, templates

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE = SESSION_COOKIE


def _settings(request: Request) -> Settings:
    return request.app.state.settings


async def _issue_session(response: RedirectResponse, request: Request, uow: UnitOfWork, member_id: str) -> None:
    """Create a server-side session row and sign its id into the cookie."""
    sid = new_session_id()
    await uow.auth.create_session(sid, member_id)  # committed by the router's UowDep
    token = sign_session(sid, _settings(request).secret)
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return HTMLResponse(_AUTH_HTML.format(title="Log in", action="/auth/login", submit="Log in",
                                          role_field="", hint="Use your Crewspace account."))


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return HTMLResponse(_AUTH_HTML.format(title="Register", action="/auth/register", submit="Create account",
                                          role_field='',
                                          hint="Create a human account."))


@router.post("/register")
async def register(
    request: Request,
    uow: UowDep,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("member"),
):
    role = "team_member"
    if await uow.auth.get_member_by_name(username):
        return HTMLResponse(_AUTH_HTML.format(title="Register", action="/auth/register", submit="Create account",
                                              role_field='',
                                              hint="<span style='color:#f38ba8'>That username is taken.</span>"), status_code=409)
    member_id = f"user_{uuid.uuid4().hex[:8]}"
    await uow.auth.create_member(member_id, "human", username, password, role, avatar="🧑")
    response = RedirectResponse("/", status_code=303)
    await _issue_session(response, request, uow, member_id)
    return response


@router.post("/login")
async def login(
    request: Request,
    uow: UowDep,
    username: str = Form(...),
    password: str = Form(...),
):
    member = await uow.auth.get_member_by_name(username)
    if not member or not member["password_hash"] or not await uow.auth.verify_password(member["id"], password):
        return HTMLResponse(_AUTH_HTML.format(title="Log in", action="/auth/login", submit="Log in", role_field="",
                                              hint="<span style='color:#f38ba8'>Invalid username or password.</span>"), status_code=401)
    response = RedirectResponse("/", status_code=303)
    await _issue_session(response, request, uow, member["id"])
    return response


@router.post("/logout")
async def logout(request: Request, uow: UowDep):
    token = request.cookies.get(COOKIE)
    if token:
        sid = unsign_session(token, _settings(request).secret)
        if sid:
            await uow.auth.delete_session(sid)
    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@router.get("/agents/register", response_class=HTMLResponse)
async def agent_register_page(
    request: Request, current_user: CurrentUserDep, uow: UowDep
):
    if current_user is None or current_user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can register agents")
    return templates.TemplateResponse(
        request=request,
        name="agent_register.html",
        context={
            "request": request,
            "current_user": current_user,
            "agents": await uow.auth.list_members(kind="agent"),
            **await navigation_context(uow, current_user),
        },
    )


@router.post("/agents/register")
async def agent_register(
    request: Request,
    uow: UowDep,
    current_user: CurrentUserDep,
    name: str = Form(...),
    avatar: str = Form("🤖"),
    base_url: str = Form(""),
    backend: str = Form("stub"),
):
    """Register a bot/agent member (admin only).

    Generates an Ed25519 keypair for the agent: the PUBLIC key is stored with the
    member (server-side, used to verify the agent's connect claim + signed
    actions); the PRIVATE key is shown to the registering admin exactly once and
    must be copied into the agent process's config. The agent uses it to sign its
    connect claim and every action it takes. This is the Buzz model: each agent
    has its own identity and a verifiable, non-repudiable audit trail.

    ``backend`` selects how the *in-app* fallback agent behaves when this agent is
    not connected over WebSocket: ``stub`` (canned) or ``llm`` (uses the server's
    CREWSPACE_LLM_* credentials, which live in the environment — never in the DB). A
    remote (connected) agent runs its own logic/LLM in its own process, so its key
    is never shared with the app at all.
    """
    if current_user is None or current_user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can register agents")

    from ...security import generate_agent_keypair

    backend = backend if backend in ("stub", "llm") else "stub"
    priv_b64u, pub_b64u = generate_agent_keypair()
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_") or uuid.uuid4().hex[:6]
    member_id = f"agent_{slug}"
    if await uow.auth.get_member(member_id):
        member_id = f"agent_{uuid.uuid4().hex[:8]}"
    await uow.auth.register_member(
        member_id, name.strip(), "agent", avatar or "🤖", "agent", base_url.strip() or None, pub_b64u, backend
    )
    ws_url = f"ws://{_settings(request).host}:{_settings(request).port}/agents/ws"
    return templates.TemplateResponse(
        request=request,
        name="agent_registered.html",
        context={
            "name": name.strip(), "agent_id": member_id, "private_key": priv_b64u,
            "ws_url": ws_url, "backend": backend,
        },
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


# --- minimal inline pages (replace with real templates later) --------------
_AGENT_REGISTER_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Register agent — Crewspace</title>
<style>body{font:15px/1.5 system-ui,sans-serif;background:#1e1e2e;color:#cdd6f4;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#313244;padding:28px 32px;border-radius:12px;width:340px}
input,button{width:100%;margin:8px 0;padding:9px;border-radius:7px;border:1px solid #45475a;
background:#181825;color:#cdd6f4;font:inherit}
button{background:#89b4fa;color:#11111b;border:0;font-weight:600;cursor:pointer}
h2{margin:0 0 4px}.actions{display:flex;gap:8px}.actions button,.actions .cancel{width:50%}
.cancel{display:block;margin:8px 0;padding:9px;text-align:center;border-radius:7px;border:1px solid #45475a;color:#cdd6f4;text-decoration:none}
.hint{font-size:12px;opacity:.8;margin-top:6px}</style></head>
<body><div class="card">
<h2>Register agent</h2>
<form method="post" action="/auth/agents/register">
<input name="name" placeholder="Agent name (e.g. Coder)" required />
<input name="avatar" placeholder="Avatar emoji (🤖)" value="🤖" />
<input name="base_url" placeholder="Base URL if remote (leave blank = local)" />
<select name="backend">
  <option value="stub">stub (canned replies)</option>
  <option value="llm">llm (uses server CREWSPACE_LLM_* creds)</option>
</select>
<div class="actions"><a class="cancel" href="/management">Cancel</a><button type="submit">Register agent</button></div>
</form>
<div class="hint">Registered agents appear in the sidebar and can be mentioned like @name.</div>
</div></body></html>"""
_AUTH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>{title} — Crewspace</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;background:#1e1e2e;color:#cdd6f4;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{background:#313244;padding:28px 32px;border-radius:12px;width:320px}}
input,button,select{{width:100%;margin:8px 0;padding:9px;border-radius:7px;border:1px solid #45475a;
background:#181825;color:#cdd6f4;font:inherit}}
button{{background:#89b4fa;color:#11111b;border:0;font-weight:600;cursor:pointer}}
h2{{margin:0 0 4px}} .hint{{font-size:12px;opacity:.8;margin-top:6px}}</style></head>
<body><div class="card">
<h2>{title}</h2>
<form method="post" action="{action}">
<input name="username" placeholder="Username" required />
<input name="password" type="password" placeholder="Password" required />
{role_field}
<button type="submit">{submit}</button>
</form>
<div class="hint">{hint}</div>
</div></body></html>"""
