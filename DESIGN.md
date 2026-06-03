# MCP Server Design

## Architecture

```
Claude (MCP client)
    │
    │  POST /{project_id}/mcp
    ▼
Starlette (outer app)
    │
    ├── AuthMiddleware          extracts project_id, validates JWT
    │
    ├── GET  /.well-known/oauth-authorization-server   → proxies Auth0 OIDC metadata
    ├── GET  /.well-known/oauth-protected-resource/{project_id}/mcp
    ├── GET  /{project_id}/authorize                   → proxies Auth0 /authorize
    ├── POST /{project_id}/token                       → proxies Auth0 /oauth/token
    │
    └── Mount("/{project_id}")
            │
            └── FastMCP (inner ASGI app)
                    └── POST /mcp   ← actual MCP protocol handler
```

---

## The Lifespan Problem

### What ASGI lifespan is

Every ASGI app can implement a **lifespan** — startup and shutdown hooks that run once before the first request and once after the last. The ASGI server (uvicorn) drives this by sending a special `{"type": "lifespan.startup"}` event to the app before serving any HTTP traffic. The app does its initialization, then replies with `{"type": "lifespan.startup.complete"}`.

FastMCP uses lifespan to start an `anyio` task group inside `StreamableHTTPSessionManager.run()`. Every incoming MCP request is handled inside that task group. If the task group hasn't been initialized, FastMCP raises:

```
RuntimeError: Task group is not initialized. Make sure to use run().
```

### Why mounting breaks it

When we wrap FastMCP inside a Starlette app using `Mount("/{project_id}", app=mcp_asgi)`, the **outer** Starlette app owns the lifespan. Uvicorn sends the startup event to the outer app only. Starlette's default lifespan handler runs its own `on_startup` hooks but **does not forward the lifespan event to mounted sub-apps**. So FastMCP's lifespan never fires, its task group is never created, and every request crashes with the error above.

### The fix: manual lifespan forwarding

We drive FastMCP's lifespan ourselves from our outer app's lifespan using the raw ASGI protocol. The ASGI lifespan protocol works via a pair of async callables (`receive` and `send`) that the app calls to exchange events:

```
our lifespan                          FastMCP lifespan handler
     │                                         │
     │── receive_queue.put(startup) ──────────▶│
     │                                         │  (initializes task group)
     │◀── send_queue.put(startup.complete) ────│
     │                                         │
     │         (yield — server runs)           │
     │                                         │
     │── receive_queue.put(shutdown) ──────────▶│
     │                                         │  (cleans up task group)
     │◀── send_queue.put(shutdown.complete) ───│
```

In code:

```python
@asynccontextmanager
async def lifespan(app):
    receive_queue: asyncio.Queue = asyncio.Queue()
    send_queue: asyncio.Queue = asyncio.Queue()

    # 1. Put the startup event into the queue FastMCP will read from
    await receive_queue.put({"type": "lifespan.startup"})

    # 2. Run FastMCP's lifespan concurrently as a background task
    task = asyncio.create_task(
        mcp_asgi(
            {"type": "lifespan", "asgi": {"version": "3.0"}},
            receive_queue.get,   # FastMCP calls this to receive events from us
            send_queue.put,      # FastMCP calls this to send events back to us
        )
    )

    # 3. Wait until FastMCP signals startup is done
    await send_queue.get()  # lifespan.startup.complete

    try:
        yield  # server is now live, requests are served
    finally:
        # 4. Signal shutdown and wait for FastMCP to clean up
        await receive_queue.put({"type": "lifespan.shutdown"})
        await send_queue.get()  # lifespan.shutdown.complete
        await task
```

### Concepts: task group and lifespan are not FastMCP-specific

**Task group** is a structured concurrency primitive from `anyio` (also available as `asyncio.TaskGroup` in Python 3.11+). It has nothing to do with FastMCP or Starlette — it groups concurrent tasks so that if one fails, the rest are cancelled. FastMCP uses it internally to manage MCP sessions.

**Lifespan** is defined by the **ASGI spec**, not by any library. Uvicorn implements the driver side (sends the events). Starlette implements the receiver side (exposes `lifespan=` parameter). FastMCP uses Starlette's lifespan hook to start its task group at the right moment — when the event loop is already running but no requests have arrived yet.

| Concept | Defined by | Implemented by | Used by |
|---|---|---|---|
| Task group | anyio / structured concurrency | anyio (or `asyncio.TaskGroup`) | FastMCP internals |
| Lifespan | ASGI spec | Uvicorn (driver), Starlette (receiver) | FastMCP via Starlette |

The **manual lifespan forwarding** in our fix IS FastMCP-specific in the sense that it works around a FastMCP architectural choice (requiring lifespan to initialize its session manager). If FastMCP initialized lazily on first request instead, none of this would be needed.

---

## Simpler Alternative: Path-Stripping Middleware

The root cause of the lifespan complexity is that we **mounted** FastMCP as a sub-app. If we keep FastMCP as the **root app** instead and strip the `/{project_id}` prefix in a raw ASGI middleware, the lifespan works naturally — uvicorn sends the startup event directly to FastMCP and nothing needs to be forwarded.

```
Client request: POST /test-project/mcp
                        │
              AuthMiddleware          sees /test-project/mcp, extracts project_id
                        │
        StripPrefixMiddleware         rewrites path to /mcp
                        │
              FastMCP router          handles /mcp  ← its own lifespan already running
```

In code:

```python
class StripProjectPrefixMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            # Leave /.well-known/* paths alone — they are global, not project-scoped
            if not path.startswith("/.well-known"):
                m = re.match(r"^/([^/]+)(/.+)$", path)
                if m:
                    scope = dict(scope)
                    scope["path"] = m.group(2)
                    scope["raw_path"] = m.group(2).encode()
        await self.app(scope, receive, send)


def create_app():
    app = mcp.streamable_http_app()   # FastMCP IS the root app — lifespan works naturally
    app.add_middleware(StripProjectPrefixMiddleware)
    app.add_middleware(AuthMiddleware)
    return app
```

The `@mcp.custom_route` decorator registers routes directly on the FastMCP Starlette app, so after prefix stripping they resolve correctly:

| Incoming path | After stripping | Handled by |
|---|---|---|
| `POST /proj/mcp` | `POST /mcp` | FastMCP session handler |
| `GET /proj/authorize` | `GET /authorize` | `@mcp.custom_route("/authorize")` |
| `POST /proj/token` | `POST /token` | `@mcp.custom_route("/token")` |
| `GET /.well-known/oauth-authorization-server` | unchanged | `@mcp.custom_route(...)` |
| `GET /.well-known/oauth-protected-resource/proj/mcp` | unchanged | `@mcp.custom_route(...)` |

**Trade-off vs current approach**: simpler lifespan, but all routes live inside FastMCP via `@mcp.custom_route` rather than in a separate Starlette app. The current approach (outer Starlette + Mount) gives a cleaner separation between MCP routes and infrastructure routes, at the cost of the lifespan workaround.

---

## OAuth Flow

```
1. Claude → POST /{project_id}/mcp
           ← 401  WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource/{project_id}/mcp"

2. Claude → GET /.well-known/oauth-protected-resource/{project_id}/mcp
           ← {"resource": AUDIENCE, "authorization_servers": ["https://{AUTH0_DOMAIN}/"]}

3. Claude → GET /.well-known/oauth-authorization-server
           ← (proxied from Auth0 /.well-known/openid-configuration)
             {"authorization_endpoint": "...", "token_endpoint": "...", ...}

4. Claude → Auth0 /authorize   (user logs in)
           ← authorization code

5. Claude → POST /{project_id}/token  (or directly to Auth0)
           ← access token (JWT, audience = AUDIENCE)

6. Claude → POST /{project_id}/mcp
             Authorization: Bearer <token>
           ← 200  (MCP session starts)
```

The `Authorization: Bearer` token is a JWT signed by Auth0. `AuthMiddleware` validates:
- Signature against Auth0's JWKS (`/{AUTH0_DOMAIN}/.well-known/jwks.json`)
- `aud` claim matches `AUDIENCE`
- `iss` claim matches `https://{AUTH0_DOMAIN}/`

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `AUTH0_DOMAIN` | yes | Auth0 tenant domain, e.g. `tenant.us.auth0.com` |
| `AUDIENCE` | no | API Identifier registered in Auth0. Defaults to `{SERVER_URL}/`. Must match exactly what is registered in Auth0 under Applications → APIs. |
| `SERVER_URL` | no | Public URL of this server. Used in `WWW-Authenticate` metadata URLs so Claude can reach them. Defaults to `http://localhost:3000`. |

### Common mistakes

- **`AUDIENCE` not registered in Auth0** — Auth0 falls back to `userinfo` audience and issues an opaque (binary) token instead of a JWT. PyJWT fails with a UTF-8 decode error.
- **`AUDIENCE` trailing slash mismatch** — Auth0 API identifiers are compared exactly. `https://host/` and `https://host` are different.
- **Forgetting to `source .env`** — env vars from the file are not active in the current shell.

---

## Design Considerations / Future Work

- **Multi-tenant**: currently all `project_id` values share one Auth0 tenant. To support per-project tenants, replace `AUTH0_DOMAIN` / `AUDIENCE` globals with a project config map and cache one `PyJWKClient` per domain.
- **DPoP**: Auth0 supports DPoP (RFC 9449). Enabling it on the API and validating the `DPoP` proof header on the server would bind tokens to Claude's ephemeral key pair, preventing relay attacks even if a token is intercepted.
- **`azp` allowlist**: validate the `azp` claim in the JWT against a known list of client IDs to block tokens issued to unauthorized applications.
