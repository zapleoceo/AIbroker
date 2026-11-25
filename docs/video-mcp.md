# Private video MCP

AIbroker supports stateless [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
at `https://aib.zapleo.com/mcp/video`. It returns JSON, not an SSE stream. This
endpoint is for a private Broker project created by the owner, with only
`llm:video` and, if the owner has separately authorized HeyGen, `llm:video:heygen`.
It does not accept public signup keys, admin keys, multi-purpose project keys,
browser cookies, or unauthenticated calls. It does not change the SIM project's
permissions or settings. No OAuth setup is needed for a client that supports a
static bearer token.

## Connect a client

Create a separate project through the existing private project settings. Grant
only the approved video scopes and retain your normal per-project budget
controls. Keep the issued project key in your client's secret storage. Configure
the client to send it via `Authorization: Bearer` with every request, not in the
URL or in a shared configuration file. For Codex (set the environment variable
via your secret manager before starting the client):

```sh
codex mcp add broker_video --url https://aib.zapleo.com/mcp/video \
  --bearer-token-env-var AIBROKER_VIDEO_PROJECT_KEY
```

Initialize, list tools, and run `video_providers` to check the connection
without submitting a job. The tool reports the providers the project is
allowed to use, not that a provider is funded or currently available.

## Generate and retrieve

- `video_generate` requires a provider (`gemini` or `heygen`), prompt, stable
  `idempotency_key`, and explicit `confirm_paid_generation: true`. Ask the
  user before submitting. Both use paid API video lanes, not HeyGen web
  subscription credits. There is no automatic provider switch. Retry exactly
  the same request with the same key; changing the request with an existing
  key is a conflict. Provider-specific size and duration limits still apply.
- The optional first frame is inline JPEG, PNG or WebP up to 2 MiB decoded.
  The server never fetches a caller-supplied URL. Use the existing REST route
  if the input exceeds the MCP transport limit and fits the REST contract.
- `video_status` accepts the job ID and returns a suggested interval while
  pending. When done, it provides a private HTTPS result link. Download it
  with the same project bearer in the request header; it is not a public or
  signed link, and it does not include the bearer. Result links require the
  original project and current provider grant. Media never enters MCP tool
  context as base64. Neither the status nor the result endpoint lists jobs.
  The reserved `VIDEO_MCP_WORKFLOW` is invisible to the generic REST job
  APIs, so legacy polling cannot bypass the narrower result guard.

## Code

`video_mcp` and `video_mcp_result` in `routes/video_mcp.py` authenticate every
request. `submit_video_clip` in the REST proxy shares validation, request caps,
idempotent enqueue and provider guards with the MCP endpoint. `VideoMcpJob`
and `get_video_mcp_job` select only this project's MCP video jobs and do not
read result bytes on a status poll. The route returns no provider keys,
prompts, billing configuration or raw provider errors.
