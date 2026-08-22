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
from ..deps import SESSION_COOKIE, CurrentUserDep, CurrentUserOptionalDep, UowDep
from ..rendering import navigation_context, templates

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE = SESSION_COOKIE


def _settings(request: Request) -> Settings:
    return request.app.state.settings


async def _issue_session(response: RedirectResponse, request: Request, uow: UnitOfWork, member_id: str) -> None:
    """Persist a server-side session before issuing its signed cookie."""
    sid = new_session_id()
    await uow.auth.create_session(sid, member_id)
    # The browser may follow the 303 before dependency teardown completes. Commit
    # here so the redirected request cannot race the session row becoming visible.
    await uow.commit()
    token = sign_session(sid, _settings(request).secret)
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, current_user: CurrentUserOptionalDep):
    if current_user is not None:
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(
        _AUTH_HTML.format(
            title="Log in", action="/auth/login", submit="Log in",
            role_field="", hint="Use your Crewspace account.",
        ),
        headers={"Cache-Control": "no-store"},
    )


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
    if current_user is None:
        raise HTTPException(status_code=403, detail="You must be logged in to register an agent")
    return templates.TemplateResponse(
        request=request,
        name="agent_register_form.html",
        context={
            "request": request,
            "current_user": current_user,
            "is_superadmin": current_user["role"] == "superadmin",
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
    uses_app_llm: str = Form(""),
):
    """Register an agent member.

    Two kinds of agents:

    * **Remote (WebSocket) agent** — any logged-in user may create one. The app
      generates an Ed25519 keypair; the PUBLIC key is stored and the PRIVATE key
      is shown once for the agent process to connect with. The agent runs as its
      own process and uses its own LLM.
    * **Builtin app-LLM agent** — only a superadmin may create one
      (``uses_app_llm=1``). It runs inside the main app using the server's
      ``CREWSPACE_LLM_*`` credentials, has no keypair, and is never WS-connected.
    """
    if current_user is None:
        raise HTTPException(status_code=403, detail="You must be logged in to register an agent")

    wants_app_llm = (uses_app_llm or "").strip().lower() in ("1", "on", "true", "yes")
    if wants_app_llm and current_user["role"] != "superadmin":
        raise HTTPException(
            status_code=403,
            detail="Only superadmins can create a builtin agent that uses the main app LLM",
        )

    from ...security import generate_agent_keypair

    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_") or uuid.uuid4().hex[:6]
    member_id = f"agent_{slug}"
    if await uow.auth.get_member(member_id):
        member_id = f"agent_{uuid.uuid4().hex[:8]}"

    if wants_app_llm:
        # Builtin agent: runs in-process with the server's LLM; no keypair.
        await uow.auth.register_member(
            member_id, name.strip(), "agent", avatar or "🤖", "agent",
            None, pubkey=None, backend="llm", uses_app_llm=1,
        )
        await uow.commit()
        return templates.TemplateResponse(
            request=request,
            name="agent_registered.html",
            context={
                "name": name.strip(), "agent_id": member_id, "private_key": None,
                "ws_url": "", "backend": "llm", "uses_app_llm": True,
            },
            headers={
                "Cache-Control": "no-store", "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            },
        )

    # Remote agent: generate a keypair; the private key is shown exactly once.
    priv_b64u, pub_b64u = generate_agent_keypair()
    await uow.auth.register_member(
        member_id, name.strip(), "agent", avatar or "🤖", "agent",
        None, pub_b64u, backend="stub", uses_app_llm=0,
    )
    await uow.commit()
    ws_url = f"ws://{_settings(request).host}:{_settings(request).port}/agents/ws"
    return templates.TemplateResponse(
        request=request,
        name="agent_registered.html",
        context={
            "name": name.strip(), "agent_id": member_id, "private_key": priv_b64u,
            "ws_url": ws_url, "backend": "remote", "uses_app_llm": False,
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
