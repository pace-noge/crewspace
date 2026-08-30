"""API dependency wiring (FastAPI Depends).

Thin adapters between the web framework and the application/infrastructure layers:
  * get_uow      -> a request-scoped UnitOfWork (closed by Database.uow()).
  * get_agent    -> the configured agent (singleton from settings).
  * get_registry -> the Tool Registry (singleton).
Services are constructed per request from these singletons.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..application.services import BoardService, ChatService
from ..application.tools import ToolRegistry, build_registry
from ..application.workspace_service import WorkspaceService
from ..config import Settings, get_settings
from ..domain.ports import UnitOfWork
from ..infrastructure.mcp_client import build_external_tool_executor
from .board_live import build_board_delta_publisher
from ..security import unsign_session

SESSION_COOKIE = "crewspace_session"


def get_settings_dep() -> Settings:
    return get_settings()


def get_registry_dep() -> ToolRegistry:
    # Built once per process; tools are stateless. The board-delta publisher
    # wires live updates for agent-originated mutations.
    return build_registry(publisher=build_board_delta_publisher())


async def get_uow(request: Request) -> AsyncIterator[UnitOfWork]:
    db = request.app.state.db
    async with db.uow() as uow:
        yield uow


UowDep = Annotated[UnitOfWork, Depends(get_uow)]
RegistryDep = Annotated[ToolRegistry, Depends(get_registry_dep)]


def get_chat_service(registry: RegistryDep, settings: Annotated[Settings, Depends(get_settings_dep)]) -> ChatService:
    return ChatService(
        registry, settings, mcp_executor_factory=build_external_tool_executor
    )


def get_board_service(registry: RegistryDep, settings: Annotated[Settings, Depends(get_settings_dep)]) -> BoardService:
    return BoardService(registry, settings)


def get_workspace_service() -> WorkspaceService:
    return WorkspaceService()


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
BoardServiceDep = Annotated[BoardService, Depends(get_board_service)]
WorkspaceServiceDep = Annotated[WorkspaceService, Depends(get_workspace_service)]


def _secret(request: Request) -> str:
    return request.app.state.settings.secret


async def get_current_member(request: Request, uow: UowDep) -> dict | None:
    """Resolve the logged-in member from the signed session cookie, or None."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    sid = unsign_session(token, _secret(request))
    if not sid:
        return None
    return await uow.auth.get_session_member(sid)


async def get_current_user(request: Request, uow: UowDep) -> dict:
    """Return the authenticated acting user or reject the request."""
    member = await get_current_member(request, uow)
    if member is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return member


CurrentUserDep = Annotated[dict, Depends(get_current_user)]

# Optional variant for page routes: lets them redirect anonymous browsers to
# the login page instead of raising a raw 401 JSON body.
CurrentUserOptionalDep = Annotated[dict | None, Depends(get_current_member)]

LOGIN_REDIRECT = "/auth/login"


def require_member_redirect(current_user: dict | None) -> RedirectResponse | None:
    """For HTML page routes: return a redirect to login if anonymous, else None.

    Unauthenticated browsers hitting a page route should be sent to the login
    page, not shown a ``401 {"detail":"Authentication required"}`` JSON body.
    API/JSON endpoints keep using ``CurrentUserDep`` and continue to 401.
    """
    if current_user is None:
        return RedirectResponse(LOGIN_REDIRECT, status_code=303)
    return None
