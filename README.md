# Robi Paperless-NGX MCP

A [FastMCP](https://gofastmcp.com) server that lets MCP clients such as Claude Code and Claude Desktop search, read, create, update and delete everything in a [Paperless-NGX](https://github.com/paperless-ngx/paperless-ngx) instance. It can also upload large files (PDFs up to 100 MB) and ingest documents straight from web URLs. It runs in Docker on your LAN, and clients authenticate with a bearer token.

Tested against **Paperless-NGX v3.4.x** (REST API version 10). For 2.x, set `PAPERLESS_API_VERSION=9`.

## Tools

| Area | Tools |
|---|---|
| Documents | `search_documents`, `get_document`, `get_document_metadata`, `get_document_suggestions`, `get_document_history`, `find_similar_documents`, `get_document_thumbnail`, `download_document`, `get_next_asn`, `update_document`, `delete_document` |
| Trash | `list_trash`, `restore_documents`, `empty_trash` |
| Adding files | `upload_document` (small, base64), `create_upload_link` (large local files), `upload_document_from_url`, `upload_document_from_path` (stdio only), `get_link_status` |
| Large downloads | `create_download_link` |
| Notes | `list_notes`, `add_note`, `delete_note` |
| Bulk & PDF | `bulk_edit_documents`, `bulk_delete_documents`, `reprocess_documents`, `rotate_documents`, `merge_documents`, `split_document`, `delete_pages`, `edit_document_pages` |
| Tags | `list_tags`, `get_tag`, `create_tag`, `update_tag`, `delete_tag` |
| Correspondents | `list_correspondents`, `get_correspondent`, `create_correspondent`, `update_correspondent`, `delete_correspondent` |
| Document types | `list_document_types`, `get_document_type`, `create_document_type`, `update_document_type`, `delete_document_type` |
| Storage paths | `list_storage_paths`, `get_storage_path`, `create_storage_path`, `update_storage_path`, `delete_storage_path` |
| Custom fields | `list_custom_fields`, `get_custom_field`, `create_custom_field`, `update_custom_field`, `delete_custom_field` |
| Saved views | `list_saved_views`, `get_saved_view`, `create_saved_view`, `update_saved_view`, `delete_saved_view` |
| Share links | `list_share_links`, `create_share_link`, `delete_share_link` |
| Workflows | `list_workflows`, `get_workflow`, `create_workflow`, `update_workflow`, `delete_workflow` |
| Mail | `list_mail_accounts`, `create_mail_account`, `update_mail_account`, `delete_mail_account`, `process_mail_account`, `list_mail_rules`, `get_mail_rule`, `create_mail_rule`, `update_mail_rule`, `delete_mail_rule` |
| Tasks & reporting | `get_task`, `list_tasks`, `acknowledge_tasks`, `get_statistics`, `get_server_status` |

The server also exposes these resources: `paperless://tags`, `paperless://correspondents`, `paperless://document-types`, `paperless://storage-paths` and `paperless://custom-fields`.

Things to know:
- **Names or ids.** Wherever a tool takes a tag, correspondent, document type, storage path or custom field, you can pass its exact name (case-insensitive) or its id. Select-type custom fields also accept the option label.
- **Updates only change what you send.** `update_document` also has `add_tags` and `remove_tags`. Custom fields are merged, and passing `null` as a value removes that field from the document.
- **Deleting a document moves it to the trash.** `restore_documents` brings it back. `empty_trash`, `delete_pages`, `merge_documents` (with `delete_originals`) and the other PDF edits are permanent, and they are marked destructive (`destructiveHint`) so clients can ask for confirmation first.
- **Consumption runs in the background.** Upload tools wait up to `wait_seconds` (30 by default) for Paperless to finish, then return the new `document_id`. If it takes longer, they return a `task_id` to check with `get_task`. Rejected duplicates are reported along with the existing document's id.
- **Read-only mode.** Setting `MCP_READ_ONLY=true` registers only the search, get, list, report and download tools.
- **Not exposed on purpose:** user and group admin, application config, and the AI chat endpoints.

## Large files

A 100 MB PDF can't travel as a tool argument: base64 would make it about 133 MB of JSON that the model has to write out. Instead, the file bypasses the model.

**From your computer: upload links.**
1. The model calls `create_upload_link(filename="scan.pdf", tags=["Invoice"], ...)`.
2. That returns a single-use URL and a ready-to-run command:
   ```bash
   curl -fsS -T scan.pdf http://192.168.1.20:8000/uploads/<token>
   ```
3. Claude Code (or you) runs the command. The file streams to disk on the server, is type-checked, and is streamed on to Paperless along with the metadata you gave. curl prints the result (`document_id`, or a `task_id` if Paperless is still processing). If that output is lost, `get_link_status(slot_id)` returns the same result.

Links expire after `MCP_UPLOAD_SLOT_TTL` (15 minutes), work once, and are capped at `MCP_MAX_UPLOAD_MB` (100). Names in the metadata are checked when the link is created, so a typo fails before you upload 100 MB.

**From the web: `upload_document_from_url`.** The server downloads the file itself, streaming it to disk, and then hands it to Paperless. The URL must serve the file itself. If it returns an HTML page instead, the call is refused and the error asks for the direct link. Downloads are protected against server-side request forgery:
- only `https://` URLs are allowed;
- private, LAN, loopback and cloud-metadata addresses are refused;
- DNS is resolved once and pinned;
- every redirect (up to 3) is checked again;
- the size cap is enforced while streaming.

Set `MCP_ALLOW_URL_FETCH=false` to remove the tool.

**Large downloads: `create_download_link`.** This returns a single-use `curl -o` command that streams a document of any size from Paperless through the MCP server. `download_document` returns files inline instead, but only up to `MCP_MAX_INLINE_MB` (10).

**Local stdio mode: `upload_document_from_path`.** This reads a file directly from disk. Use `MCP_LOCAL_UPLOAD_ROOTS` to restrict which folders it may read.

Staging files go to `MCP_UPLOAD_DIR`, which is a 512 MB tmpfs in the Compose file, and are deleted as soon as Paperless has them. At most `MCP_MAX_CONCURRENT_UPLOADS` transfers run at once.

## Quick start (Docker)

```bash
cp .env.example .env
# Edit .env: set PAPERLESS_URL, PAPERLESS_TOKEN, MCP_PUBLIC_URL, and a client token:
openssl rand -hex 32   # paste into MCP_AUTH_TOKENS
docker compose up -d --build
curl http://localhost:8000/health    # {"status":"ok","paperless":"ok"}
```

Create a dedicated Paperless user for the MCP server and use its API token (open the profile menu, then My Profile, then API Auth Token). Everything the MCP server does is recorded as that user, and the server only sees the documents that user is allowed to see.

`MCP_PUBLIC_URL` must be the address clients use to reach this server, for example `https://paperless-mcp.lan`. Upload and download links are built from it. If it isn't set, links are built from the address the client connected to.

If Paperless runs in another Compose project on the same host, uncomment the `networks` block in `docker-compose.yml` and set `PAPERLESS_URL=http://webserver:8000`.

### Throwaway dev stack

```bash
echo "DEV_ADMIN_PASSWORD=$(openssl rand -hex 12)" >> .env
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

This starts Paperless at http://localhost:8010, using the admin login from `.env`, and the MCP server at http://localhost:8000/mcp.

## Connecting clients

The MCP endpoint is `http://<server-lan-ip>:8000/mcp`. Every request must include the header `Authorization: Bearer <one of MCP_AUTH_TOKENS>`.

**Claude Code**
```bash
claude mcp add --transport http paperless http://192.168.1.20:8000/mcp \
  --header "Authorization: Bearer $PAPERLESS_MCP_TOKEN"
```

**Claude Desktop / other JSON-configured clients**
```json
{
  "mcpServers": {
    "paperless": {
      "type": "http",
      "url": "http://192.168.1.20:8000/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```
Upload links need a client that can run shell commands, such as Claude Code. Clients without a shell can still use `upload_document_from_url` and small base64 uploads.

**MCP Inspector:** run `npx @modelcontextprotocol/inspector`, choose "Streamable HTTP", and add the Authorization header.

**Local stdio** (no network, no token): set `MCP_TRANSPORT=stdio` and run `uv run paperless-mcp`.

### Tool search (large tool lists)

With more than 80 tools, sending every schema up front costs context. There are two ways around that:

- **Client-side, recommended.** Claude Code and the Claude API's tool search load MCP tools on demand. They read only names and descriptions until a tool is needed, so nothing needs configuring on the server.
- **Server-side, for clients without tool search.** Set `MCP_TOOL_SEARCH=bm25` (or `regex`). The client then sees only `search_documents`, `get_document`, `search_tools` and `call_tool`, and finds everything else through `search_tools`. The trade-off: other tools run through the generic `call_tool` proxy, so per-tool annotations such as `destructiveHint` and per-tool client permissions no longer apply to them. Pair this mode with `MCP_READ_ONLY=true`, or with a client that confirms every call.

## Security notes

- **Tokens.** Tokens must be at least 16 characters, and comparison is constant-time. You can list several comma-separated tokens, for example one per client or for rotation.
- **Unauthenticated routes.** `/health` reveals nothing beyond up/down. `/uploads/<token>` and `/downloads/<token>` are authorised by their 256-bit single-use token, which expires after `MCP_UPLOAD_SLOT_TTL`. Those tokens can appear in reverse-proxy access logs, but by then they have been used or have expired.
- **Use TLS.** Over plain HTTP, anyone on the network path can read the bearer token and the documents. Put the server behind a TLS reverse proxy, or keep it on a trusted VLAN. For example, with Caddy and its internal CA:
  ```caddyfile
  paperless-mcp.lan {
      tls internal
      reverse_proxy paperless-mcp:8000
  }
  ```
  Caddy has no request-body limit by default. Behind nginx, set `client_max_body_size 101m;` (or more) and `proxy_request_buffering off;` so 100 MB uploads get through.
- **Host allowlist.** `MCP_ALLOWED_HOSTS` restricts which `Host` headers are accepted. This protects against DNS rebinding.
- **Least privilege.** Use a dedicated Paperless account, and set `MCP_READ_ONLY=true` for clients that only need to look things up. `get_server_status` strips connection URLs, and mail account passwords are never returned.
- **Share links are public.** Anyone with a link from `create_share_link` can download that document without logging in. Links expire after 7 days by default. Set `PAPERLESS_PUBLIC_URL` so that the links use the address people open Paperless at, rather than an internal Docker hostname.
- **Container hardening.** The container runs as a non-root user with a read-only filesystem and no Linux capabilities. The only writable path is the staging tmpfs.

## Configuration

See [.env.example](.env.example) for every variable. Each secret (`PAPERLESS_TOKEN`, `PAPERLESS_PASSWORD`, `MCP_AUTH_TOKENS`) can also be read from a file named in a `*_FILE` variable.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Layout: `src/paperless_mcp/`
- `client.py`: Paperless HTTP client (token or basic auth, API versioning, pagination, name→id resolution, streaming)
- `ingest.py`: the shared upload pipeline (type detection, metadata, `post_document`, task polling)
- `uploads.py`: single-use upload and download links and their HTTP routes
- `fetch.py`: SSRF-guarded streaming URL download
- `auth.py`: bearer-token verifier
- `server.py`: app factory, `/health` and resources
- `tools/`: one module per area
