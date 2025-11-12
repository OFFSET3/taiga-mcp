# Taiga MCP Server

## Project Overview
- Implements a Starlette-based Model Context Protocol (MCP) server that exposes both Server-Sent Events (SSE) and Streamable HTTP transports for ChatGPT and other MCP compliant clients.
- Serves as a bridge between ChatGPT and the Taiga project management API, currently including an `echo` tool for transport validation with ongoing integration of Taiga-specific actions.
- Deployed as a container workload to Azure Container Apps and packaged for distribution through GitHub Container Registry (GHCR).

## Configuration
- Copy `.env.example` to `.env` (or export the variables directly) and fill in values for your Taiga instance, credentials, and deployment settings.
- Never commit `.env` files or secrets; the project `.gitignore` excludes common patterns by default.
- Local scripts and helper apps read the same variables (`TAIGA_BASE_URL`, `TAIGA_USERNAME`, `TAIGA_PASSWORD`, `ACTION_PROXY_API_KEY`, `MCP_URL`, `TAIGA_PROXY_BASE_URL`, and friends), so updating the environment keeps the entire toolchain aligned.

## Runtime Architecture
- `app.py` instantiates `FastMCP` with the Taiga MCP identity, mounts SSE at `/sse/` and Streamable HTTP at `/mcp/`, and disables trailing-slash redirects to preserve MCP session headers.
- A custom middleware rewrites bare `/mcp` requests to `/mcp/` and normalizes blank paths inside the sub-application to avoid 307 redirects that previously dropped MCP session headers.
- The FastMCP session manager is started via Starlette's lifespan hook to ensure streamable sessions stay active for long-running ChatGPT conversations.
- Health checks:
  - `GET /` → plain text "Taiga MCP up" for quick availability probes.
  - `GET /healthz` → minimal health endpoint for container orchestrators.

## Endpoints Summary
- `/` — Root status page.
- `/healthz` — Liveness probe used by Azure Container Apps.
- `/sse/` — Server-Sent Events transport; requires `Accept: text/event-stream` and returns the message posting endpoint in the first event payload.
- `/sse/sse/messages/` — POST target for SSE message submission (returned in SSE `endpoint` event).
- `/mcp/` — Streamable HTTP transport; clients **must** send `Accept: application/json, text/event-stream` to satisfy protocol negotiation.

## MCP Tools
- `echo(message)` — diagnostic helper that returns the provided message.
- `taiga.projects.list(search?)` — lists projects where the service account is a member (optional case-insensitive substring search, scoped by the authenticated user id).
- `taiga.projects.get(project_id?, slug?)` — fetches a project record either by numeric identifier or slug (one of the two inputs is required).
- `taiga.epics.list(project_id)` — lists epics for a project, including id/ref/subject/status metadata.
- `taiga.stories.list(project_id, search?, epic_id?, tags?, page?, page_size?)` — lists user stories for a project with optional filters for text search, epic membership, tags, and pagination.
- `taiga.stories.create(project_id, subject, description?, status?, tags?, assigned_to?)` — creates a Taiga user story; `status` accepts either an id or status name/slug.
- `taiga.stories.update(user_story_id, subject?, description?, status?, tags?, assigned_to?, epic_id?, milestone_id?, custom_attributes?, version?)` — updates an existing story with optimistic concurrency checks and server-side status resolution.
- `taiga.epics.add_user_story(epic_id, user_story_id)` — links a user story to an epic.
- `taiga.tasks.create(user_story_id, subject, description?, assigned_to?, status?, tags?, due_date?, idempotency_key?)` — creates a Taiga task under a story, applying status lookups and idempotency safeguards.
- `taiga.tasks.update(task_id, subject?, description?, assigned_to?, status?, tags?, due_date?, version?)` — updates a task with the same optimistic concurrency handling as stories.
- `taiga.tasks.list(project_id?, user_story_id?, assigned_to?, search?, status?, page?, page_size?)` — lists tasks with flexible filters and pagination metadata, resolving status names when a `project_id` is supplied.
- `taiga.users.list(search?)` — finds Taiga users by substring matching against full name, username, or email to support assignee resolution.
- `taiga.milestones.list(project_id, search?)` — returns milestones/sprints for a project, allowing optional name/slug filtering.

## Action Proxy Surface
- Purpose: provide a lightweight HTTP bridge for Taiga automation while MCP write tools stay allowlisted.
- Auth: every request supplies `X-Api-Key`; the value must match the `ACTION_PROXY_API_KEY` environment variable (missing/invalid keys return 401, unconfigured key returns 503).
- Endpoints:
  - `GET /actions/list_projects?search=foo` → `{ "projects": [...] }` with optional case-insensitive name filter (automatically sets `member=<service-account-id>` unless you override the `member` query parameter).
  - `GET /actions/get_project?project_id=123` → `{ "project": {...} }` returning the full Taiga project payload for the given id.
  - `GET /actions/get_project_by_slug?slug=acme-backlog` → `{ "project": {...} }` resolving the project via slug.
  - `GET /actions/list_epics?project_id=123&project_id=456` → `{ "epics": [...] }` including the originating `project_id` for each epic.
  - `GET /actions/list_stories?project_id=123&epic_id=456&search=prior+art&tag=ip` → `{ "stories": [...] }` filtered by project, epic, keywords, tags, and optional pagination (`page`/`page_size`).
  - `GET /actions/statuses?project_id=123` → `{ "statuses": [...] }` to drive status pickers.
  - `POST /actions/create_story` → `{ "story": {...} }`; accepts the same payload as the MCP tool and resolves status slugs/names.
  - `POST /actions/update_story` → `{ "story": {...} }`; accepts `story_id` plus any combination of `project_id`, `subject`, `description`, `status`, `tags`, `assigned_to` (status strings resolve to ids automatically).
  - `POST /actions/delete_story` → `{ "deleted": {"story_id": ...} }`.
  - `POST /actions/add_story_to_epic` → `{ "link": {...} }` after linking a story to an epic.
  - `POST /actions/create_epic` / `update_epic` / `delete_epic` → manage epics (`project_id`, `subject`, optional `description`, `status`, `assigned_to`, `tags`, `color`).
  - `POST /actions/create_task` / `update_task` / `delete_task` → manage tasks (`project_id`, `subject`, optional `description`, `status`, `assigned_to`, `tags`, `user_story_id`).
  - `POST /actions/create_issue` / `update_issue` / `delete_issue` → manage issues (`project_id`, `subject`, optional `description`, `status`, `priority`, `severity`, `type`, `assigned_to`, `tags`).
- Error model: JSON `{ "error": "..." }` payloads with 4xx for validation/Taiga errors and 500 for unexpected failures (also logged server-side).
- Helper scripts (require `ACTION_PROXY_API_KEY` and `TAIGA_PROXY_BASE_URL` to be set):
  - `\.\.chat-venv\Scripts\python.exe scripts/actions_proxy_client.py --pretty list-projects`
  - `\.\.chat-venv\Scripts\python.exe scripts/actions_proxy_client.py get-project --project-id <ID>`
  - `\.\.chat-venv\Scripts\python.exe scripts/actions_proxy_client.py get-project-by-slug --slug <slug>`
  - `powershell.exe -File scripts/actions-proxy.ps1 list-projects`
- Raw curl samples:
  - `curl.exe -H "X-Api-Key: $env:ACTION_PROXY_API_KEY" "$env:TAIGA_PROXY_BASE_URL/actions/list_projects?search=beta"`
  - `curl.exe -H "X-Api-Key: $env:ACTION_PROXY_API_KEY" -H "Content-Type: application/json" -d "{\"project_id\":123,\"subject\":\"Story\"}" "$env:TAIGA_PROXY_BASE_URL/actions/create_story"`

## Local Development Workflow
- **Prerequisites**
  - Python 3.11 (project uses a `.chat-venv` virtual environment by default).
  - Docker Desktop for container builds.
  - Azure CLI for deployment automation.
  - GHCR authentication (`docker login ghcr.io`).
- **Install dependencies**
  - `python -m venv .chat-venv`
  - `.\.chat-venv\Scripts\python.exe -m pip install -r requirements.txt`
- **Configure environment**
  - `copy .env.example .env` (PowerShell: `Copy-Item .env.example .env`) and update the values to point at your Taiga instance.
- **Run the server locally**
  - `.\.chat-venv\Scripts\uvicorn.exe app:app --host 127.0.0.1 --port 8010`
  - Streamable probe: `.\.chat-venv\Scripts\python.exe streamable_client.py http://127.0.0.1:8010/mcp --message "hello local"`
- **SSE manual test**
  - `curl.exe -sN -H "Accept: text/event-stream" http://127.0.0.1:8010/sse/`

## Container Build & Publish
- Pick an image name (for example `ghcr.io/your-org/taiga-mcp`) and set:
  - PowerShell: `$env:CONTAINER_IMAGE = 'ghcr.io/your-org/taiga-mcp'` and `$env:IMAGE_TAG = 'v0.0.27'`
  - Bash: `export CONTAINER_IMAGE=ghcr.io/your-org/taiga-mcp` and `export IMAGE_TAG=v0.0.27`
- Build tagged images:
  - `docker build -t "$CONTAINER_IMAGE:$IMAGE_TAG" -t "$CONTAINER_IMAGE:latest" .`
- Push to your registry:
  - `docker push "$CONTAINER_IMAGE:$IMAGE_TAG"`
  - `docker push "$CONTAINER_IMAGE:latest"`

## Azure Container Apps Deployment
- Export the following environment variables (update names to match your subscription):
  - `$env:AZURE_RESOURCE_GROUP = 'your-resource-group'`
  - `$env:AZURE_CONTAINER_APP = 'taiga-mcp'`
- Deploy the new revision (after pushing the image):
  - `az containerapp update -g $env:AZURE_RESOURCE_GROUP -n $env:AZURE_CONTAINER_APP --image "$CONTAINER_IMAGE:$IMAGE_TAG"`
- Or run the helper script (builds, pushes, and deploys in one step; honours the same env vars):
  - `python scripts/deploy_to_azure.py`
- Helpful Windows settings (avoid WinError 5 permission issues):
  - `$env:AZURE_EXTENSION_DIR = Join-Path $HOME '.az-extensions'`
  - `$env:AZURE_CONFIG_DIR = Join-Path $HOME '.az-cli'`

## Secret Management for Taiga Credentials
- Store Taiga credentials and proxy keys in your secret store of choice (Azure Container Apps secrets, Kubernetes secrets, etc.) and surface them as environment variables inside the container.
- Example (Azure Container Apps):
  - `az containerapp secret set --resource-group $env:AZURE_RESOURCE_GROUP --name $env:AZURE_CONTAINER_APP --secrets taiga-username="<USERNAME>" taiga-password="<PASSWORD>"`
  - `az containerapp update --resource-group $env:AZURE_RESOURCE_GROUP --name $env:AZURE_CONTAINER_APP --set-env-vars TAIGA_USERNAME=secretref:taiga-username TAIGA_PASSWORD=secretref:taiga-password`
- Update or rotate the action proxy key the same way:
  - `az containerapp secret set --resource-group $env:AZURE_RESOURCE_GROUP --name $env:AZURE_CONTAINER_APP --secrets action-proxy-api-key="<RANDOM_TOKEN>"`
  - `az containerapp update --resource-group $env:AZURE_RESOURCE_GROUP --name $env:AZURE_CONTAINER_APP --set-env-vars ACTION_PROXY_API_KEY=secretref:action-proxy-api-key`
- The container expects the following environment variables to be present:
  - `TAIGA_BASE_URL` — base URL for the Taiga API (typically backed by a secret reference).
  - `TAIGA_USERNAME` — service account username.
  - `TAIGA_PASSWORD` — service account password.
  - `ACTION_PROXY_API_KEY` — shared secret used by the `/actions/*` endpoints.
- Rotate credentials regularly and redeploy so that new revisions pick up the changes.

## Connecting MCP Clients
- You are expected to deploy and host your own MCP server instance (for example on Azure Container Apps, Docker Desktop, or any other Python hosting target). This repository does not expose a shared public endpoint.
- ChatGPT Custom GPT setup:
  - Open the GPT Builder UI, choose **Create**, and select **Configure** → **Add** under the Model Context Protocol tools section.
  - Enter your deployed Streamable HTTP URL (for example `https://your-domain.example/mcp`) as the endpoint and leave headers/body blank unless you have protected the route behind a proxy.
  - Save the GPT and test the `echo` tool to confirm connectivity. The session will reuse your deployed server on each invocation.
- Open-source MCP clients (such as the `mcp` Python CLI or Claude Desktop) can target the same `/mcp` endpoint; set `MCP_URL` to your deployed URL before running helper scripts like `streamable_client.py`.
- If you regenerate the container image, existing environment variables and secret references remain attached to the Container App. Only set them again when new keys/variables are introduced or when credentials rotate.

## Verification Checklist
- Streamable HTTP smoke test:
  - `\.\.chat-venv\Scripts\python.exe streamable_client.py $env:MCP_URL --message "ping"`
- SSE availability test:
  - `curl.exe -sN -H "Accept: text/event-stream" "$env:TAIGA_PROXY_BASE_URL/sse/" --max-time 5`
- Azure logs review (if deployed to ACA):
  - `az containerapp logs show -g $env:AZURE_RESOURCE_GROUP -n $env:AZURE_CONTAINER_APP --tail 50`
- ChatGPT connector validation:
  - Configure ChatGPT or another MCP client with your deployed MCP endpoint and confirm the `echo` tool responds.
  - Exercise project, epic, and story workflows end-to-end to ensure Taiga credentials and permissions are correct.

## Troubleshooting Notes
- `Not Acceptable: Client must accept text/event-stream` — Ensure clients send `Accept: application/json, text/event-stream` when calling `/mcp/`.
- `Session terminated` errors usually indicate a redirect; verify the request hits `/mcp/` (with trailing slash) and that proxies are not rewriting headers.
- Azure CLI `WinError 5` permission issues are resolved by setting `AZURE_EXTENSION_DIR` and `AZURE_CONFIG_DIR` to user-writable locations.
- After updating secrets, Azure Container Apps restarts the revision; allow 1–2 minutes before retesting endpoints.

## Testing
- Install dev dependencies: `python -m pip install -r requirements.txt pytest` (from the `.chat-venv` environment).
- Run the Python unit suite: `pytest` (covers `/actions/*` auth, validation, and Taiga error handling via fakes).
- Smoke test the helper CLI locally: `.\.chat-venv\Scripts\python.exe scripts/actions_proxy_client.py --help`.
- PowerShell validation: `powershell.exe -File scripts/actions-proxy.ps1 list-projects -BaseUrl http://127.0.0.1:8010 -ApiKey local-test` when the server is running with a dummy key.

## Request Payload Reference

### Stories
- Create (`POST /actions/create_story`): `{ "project_id": int, "subject": str, "description"?: str, "status"?: int|str, "tags"?: [str], "assigned_to"?: int }`
- Update (`POST /actions/update_story`): `{ "story_id": int, "project_id"?: int, "subject"?: str, "description"?: str, "status"?: int|str, "tags"?: [str], "assigned_to"?: int }`
- Delete (`POST /actions/delete_story`): `{ "story_id": int }`

### Epics
- Create (`POST /actions/create_epic`): `{ "project_id": int, "subject": str, "description"?: str, "status"?: int, "assigned_to"?: int, "tags"?: [str], "color"?: str }`
- Update (`POST /actions/update_epic`): `{ "epic_id": int, "subject"?: str, "description"?: str, "status"?: int, "assigned_to"?: int, "tags"?: [str], "color"?: str }`
- Delete (`POST /actions/delete_epic`): `{ "epic_id": int }`

### Tasks
- Create (`POST /actions/create_task`): `{ "project_id": int, "subject": str, "description"?: str, "status"?: int, "assigned_to"?: int, "tags"?: [str], "user_story_id"?: int }`
- Update (`POST /actions/update_task`): `{ "task_id": int, "subject"?: str, "description"?: str, "status"?: int, "assigned_to"?: int, "tags"?: [str], "user_story_id"?: int }`
- Delete (`POST /actions/delete_task`): `{ "task_id": int }`

### Issues
- Create (`POST /actions/create_issue`): `{ "project_id": int, "subject": str, "description"?: str, "status"?: int, "priority"?: int, "severity"?: int, "type"?: int, "assigned_to"?: int, "tags"?: [str] }`
- Update (`POST /actions/update_issue`): `{ "issue_id": int, "subject"?: str, "description"?: str, "status"?: int, "priority"?: int, "severity"?: int, "type"?: int, "assigned_to"?: int, "tags"?: [str] }`
- Delete (`POST /actions/delete_issue`): `{ "issue_id": int }`

## Azure AI Fallback (Future Option)
- Idea: expose Taiga access through an Azure OpenAI Assistants tool while the ChatGPT MCP allowlist is pending.
- Components: Azure Functions (or Container App) hosting the same FastMCP logic, Azure OpenAI Assistant registered with HTTPS tool endpoints, and service principal credentials stored in Key Vault.
- Status: deferred; revisit if the OpenAI allowlist remains blocked after proxy rollout. Keep scripts modular so the same helper payloads feed both the MCP proxy and any future Azure AI adapter.

## Project History (October 2025 Highlights)
- Refactored Starlette routing to eliminate automatic slash redirects that stripped MCP session headers on Azure.
- Added middleware to normalize Streamable HTTP paths and ensure compatibility with Azure ingress rewrite behavior.
- Built and published container versions `v0.0.4` through `v0.0.10`, with `v0.0.10` deployed as revision `taiga-mcp--0000016`.
- Validated SSE and Streamable HTTP transports using local clients, `curl`, and the ChatGPT MCP connector.
- Stored Taiga service account credentials securely in Azure Container Apps secrets to enable future write access to Taiga.
