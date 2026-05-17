import os
from urllib.parse import urlencode

import httpx
import jwt
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

SERVER_URL = os.getenv("SERVER_URL", "http://localhost:3000")
AUTH0_DOMAIN = os.getenv("AUTH0_DOMAIN")
if not AUTH0_DOMAIN:
    raise RuntimeError("AUTH0_DOMAIN environment variable is required")
AUDIENCE = os.getenv("AUDIENCE", SERVER_URL)

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
    client = get_jwks_client()
    key = client.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        key.key,
        algorithms=["RS256"],
        audience=AUDIENCE,
        issuer=f"https://{AUTH0_DOMAIN}/",
    )


def _unauthorized() -> Response:
    return Response(
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{SERVER_URL}/.well-known/oauth-protected-resource"'
            )
        },
    )


class AuthMiddleware(BaseHTTPMiddleware):
    _public_paths = {
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/authorize",
        "/token",
        "/oauth/token",
    }

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self._public_paths:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return _unauthorized()

        try:
            validate_token(auth.removeprefix("Bearer "))
        except Exception:
            return _unauthorized()

        return await call_next(request)


mcp = FastMCP("add-server")


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@mcp.custom_route("/", methods=["GET", "POST"])
async def root(request: Request) -> JSONResponse:
    return JSONResponse({"server": "add-server", "mcp_endpoint": "/mcp"})


@mcp.custom_route("/authorize", methods=["GET"])
async def authorize(request: Request) -> RedirectResponse:
    params = dict(request.query_params)
    params.setdefault("audience", AUDIENCE)
    return RedirectResponse(
        url=f"https://{AUTH0_DOMAIN}/authorize?{urlencode(params)}",
        status_code=302,
    )


@mcp.custom_route("/token", methods=["POST", "OPTIONS"])
@mcp.custom_route("/oauth/token", methods=["POST", "OPTIONS"])
async def token_proxy(request: Request) -> Response:
    cors_headers = {"Access-Control-Allow-Origin": "https://claude.ai",
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization"}
    if request.method == "OPTIONS":
        return Response(headers=cors_headers)
    body = await request.body()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://{AUTH0_DOMAIN}/oauth/token",
            content=body,
            headers={"Content-Type": request.headers.get("content-type", "application/x-www-form-urlencoded")},
        )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={"Content-Type": resp.headers.get("content-type", "application/json"), **cors_headers},
    )



@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_metadata(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "resource": SERVER_URL,
            "authorization_servers": [f"https://{AUTH0_DOMAIN}/"],
        }
    )


def create_app():
    app = mcp.streamable_http_app()
    app.add_middleware(AuthMiddleware)
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.getenv("PORT", "3000")),
        ssl_certfile="localhost+1.pem",
        ssl_keyfile="localhost+1-key.pem",
    )
