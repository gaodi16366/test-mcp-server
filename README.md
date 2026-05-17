# test-mcp-server

A simple MCP server with an `add` tool, protected by Auth0 JWT authentication.
Implements OAuth 2.1 Protected Resource Metadata (RFC 9728) for use with the claude.ai MCP connector.

## Prerequisites

- Python 3.11+
- [mkcert](https://github.com/FiloSottile/mkcert) for local HTTPS
- [ngrok](https://ngrok.com) to expose the server publicly
- An Auth0 tenant

## Auth0 Setup

### 1. Create an API

Go to **Auth0 → APIs → Create API**:

| Field | Value |
|---|---|
| Name | any (e.g. `mcp-api`) |
| Identifier | your public server URL (e.g. `https://xxxx.ngrok-free.app`) |

> The identifier must match your `SERVER_URL` exactly. Auth0 uses it as the JWT audience, and the server validates tokens against `SERVER_URL`. Update this whenever your public URL changes.

### 2. Create a Regular Web Application

Go to **Auth0 → Applications → Create Application → Regular Web Applications**:

**Settings tab:**

| Field | Value |
|---|---|
| Allowed Callback URLs | `https://claude.ai/api/mcp/auth_callback` |

**Advanced Settings → Grant Types tab:**

Enable:
- `Authorization Code`

**APIs tab:**

Authorize the app to access the API created in step 1. Update this each time you create a new API for a new URL.

### 3. Enable a Connection

Go to the **Connections** tab of your application and enable at least one connection (e.g. `Username-Password-Authentication`).

### 4. Create a test user

Go to **Auth0 → User Management → Users → Create User** and create a user under the `Username-Password-Authentication` connection.

### 5. Note your credentials

- **Domain** (e.g. `your-tenant.jp.auth0.com`)
- **Client ID**
- **Client Secret**

## Local Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Generate a local HTTPS certificate

```bash
brew install mkcert
mkcert -install
mkcert localhost 127.0.0.1
```

This creates `localhost+1.pem` and `localhost+1-key.pem` in the current directory.

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```
AUTH0_DOMAIN=your-tenant.region.auth0.com
SERVER_URL=https://YOUR_NGROK_URL
PORT=3000
```

### 4. Start ngrok

```bash
ngrok http https://localhost:3000 --host-header=localhost:3000
```

Copy the `https://xxxx.ngrok-free.app` URL and update `SERVER_URL` in `.env`.

### 5. Start the server

```bash
export $(cat .env | xargs)
.venv/bin/python server.py
```

## Add to Claude

1. Go to **claude.ai → Settings → Connectors → Add connector**
2. Set the MCP server URL to:
   ```
   https://YOUR_NGROK_URL/mcp
   ```
3. Enter your Auth0 **Client ID** and **Client Secret**
4. Click Connect — a browser window will open for login
5. After login, the `add` tool will appear in Claude

## Usage

Once connected, ask Claude:

> Use the add tool to calculate 3 + 5

## Notes

- When the ngrok URL changes, update `SERVER_URL` in `.env`, create a new Auth0 API with the new URL as the identifier, authorize your application to access it, and reconnect the Claude connector
- The local HTTPS cert (`*.pem`) is gitignored — each developer must generate their own with `mkcert`
