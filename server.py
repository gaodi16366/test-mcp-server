import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from urllib.parse import urlencode

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

import httpx
import jwt
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

SERVER_URL = os.getenv("SERVER_URL", "http://localhost:3000")

_project_id: ContextVar[str | None] = ContextVar("project_id", default=None)

AUTH0_DOMAIN = os.getenv("AUTH0_DOMAIN")
if not AUTH0_DOMAIN:
    raise RuntimeError("AUTH0_DOMAIN environment variable is required")
AUDIENCE = os.getenv("AUDIENCE", f"{SERVER_URL}/")

_jwks_client: jwt.PyJWKClient | None = None


def get_jwks_client() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(
            f"https://{AUTH0_DOMAIN}/.well-known/jwks.json",
            cache_keys=True,
        )
    return _jwks_client


def validate_token(token: str) -> dict:
    key = get_jwks_client().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        key.key,
        algorithms=["RS256"],
        audience=AUDIENCE,
        issuer=f"https://{AUTH0_DOMAIN}/",
    )


def _unauthorized(project_id: str) -> Response:
    metadata_url = f"{SERVER_URL}/.well-known/oauth-protected-resource/{project_id}/mcp"
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata_url}"'},
    )


# Matches /{project_id}/mcp and /{project_id}/mcp/...
_MCP_PATH_RE = re.compile(r"^/([^/]+)/mcp(?:/|$)")


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        m = _MCP_PATH_RE.match(request.url.path)
        if not m:
            return await call_next(request)

        project_id = m.group(1)

        dpop = request.headers.get("DPoP")
        logger.info(
            "project=%s method=%s path=%s DPoP=%s",
            project_id,
            request.method,
            request.url.path,
            "present" if dpop else "absent",
        )

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return _unauthorized(project_id)

        try:
            claims = validate_token(auth.removeprefix("Bearer "))
            request.state.project_id = project_id
            request.state.claims = claims
            _project_id.set(project_id)
        except Exception as e:
            logger.warning("token validation failed project=%s error=%s", project_id, e)
            return _unauthorized(project_id)

        return await call_next(request)


mcp = FastMCP("add-server")


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers."""
    project_id = _project_id.get()
    logger.info("add called project=%s a=%s b=%s", project_id, a, b)
    return a + b



async def oauth_protected_resource(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "resource": AUDIENCE,
            "authorization_servers": [f"https://{AUTH0_DOMAIN}/"],
        }
    )


async def oauth_authorization_server(request: Request) -> Response:
    # MCP clients discover OAuth endpoints via /.well-known/oauth-authorization-server (RFC 8414).
    # Auth0 exposes this under /.well-known/openid-configuration, so we proxy and return it here.
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://{AUTH0_DOMAIN}/.well-known/openid-configuration"
        )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={"Content-Type": resp.headers.get("content-type", "application/json")},
    )


async def project_authorize(request: Request) -> Response:
    params = dict(request.query_params)
    params.setdefault("audience", AUDIENCE)
    return RedirectResponse(
        url=f"https://{AUTH0_DOMAIN}/authorize?{urlencode(params)}",
        status_code=302,
    )


async def project_token_proxy(request: Request) -> Response:
    cors_headers = {
        "Access-Control-Allow-Origin": "https://claude.ai",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }
    if request.method == "OPTIONS":
        return Response(headers=cors_headers)

    body = await request.body()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://{AUTH0_DOMAIN}/oauth/token",
            content=body,
            headers={
                "Content-Type": request.headers.get(
                    "content-type", "application/x-www-form-urlencoded"
                )
            },
        )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={
            "Content-Type": resp.headers.get("content-type", "application/json"),
            **cors_headers,
        },
    )


def create_app():
    mcp_asgi = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        # Starlette does not propagate lifespan events to mounted sub-apps, so we
        # drive the FastMCP app's lifespan manually using the ASGI lifespan protocol.
        # This initializes FastMCP's StreamableHTTPSessionManager task group.
        receive_queue: asyncio.Queue = asyncio.Queue()
        send_queue: asyncio.Queue = asyncio.Queue()
        await receive_queue.put({"type": "lifespan.startup"})
        task = asyncio.create_task(
            mcp_asgi(
                {"type": "lifespan", "asgi": {"version": "3.0"}},
                receive_queue.get,
                send_queue.put,
            )
        )
        await send_queue.get()  # wait for lifespan.startup.complete
        try:
            yield
        finally:
            await receive_queue.put({"type": "lifespan.shutdown"})
            await send_queue.get()  # wait for lifespan.shutdown.complete
            await task

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route(
                "/.well-known/oauth-authorization-server",
                oauth_authorization_server,
                methods=["GET"],
            ),
            Route(
                "/.well-known/oauth-protected-resource/{project_id}/mcp",
                oauth_protected_resource,
                methods=["GET"],
            ),
            # Block /mcp from being swallowed by the /{project_id} Mount below
            Route("/mcp", lambda r: Response(status_code=404)),
            # Per-project OAuth proxy fallbacks for clients that don't do discovery
            Route("/{project_id}/authorize", project_authorize, methods=["GET"]),
            Route(
                "/{project_id}/token",
                project_token_proxy,
                methods=["POST", "OPTIONS"],
            ),
            # Must come last — /{project_id} is a wildcard prefix
            Mount("/{project_id}", app=mcp_asgi),
        ],
        middleware=[Middleware(AuthMiddleware)],
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.getenv("PORT", "3000")),
        ssl_certfile="localhost+1.pem",
        ssl_keyfile="localhost+1-key.pem",
    )
