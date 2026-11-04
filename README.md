# git_antiboycot

An **on-demand, ephemeral VPN proxy** running in a GitHub Actions job. It starts
[Xray](https://github.com/XTLS/Xray-core) with **VLESS + WebSocket + TLS**, exposes it
through a [Cloudflare quick tunnel](https://github.com/cloudflare/cloudflared), and sends
**one config-only** V2RayNG-importable link to a Telegram channel using
[python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot).

**One link is shared by multiple users.** Every device imports the same `vless://`
URI and can connect at the same time. There is no user-count input and no separate
per-user config.

No Cloudflare account, domain, certificate, or open inbound runner port is required.
This is a proxy for apps routed by V2RayNG, not a WireGuard/OpenVPN server. UDP can
be carried inside VLESS/WebSocket; this is not a raw UDP tunnel.

## What changed in this version

- **Restored the previous working WebSocket/TLS link format.** The XHTTP experiment
  was removed because it did not connect reliably in the reported environment.
- **One shared config for multiple simultaneous users.** The server has one VLESS
  UUID; VLESS allows many devices to authenticate with that same UUID concurrently.
- **End-to-end verification before publishing.** Before sending anything to Telegram,
  the workflow starts a temporary Xray client with the exact published settings and
  requires two simultaneous proxied HTTPS requests to succeed through the public tunnel.
- **Five-minute idle shutdown** instead of one hour.
- **Neutral profile remarks** and no QR code, captions, or status messages in the
  channel. The Telegram message contains exactly one `vless://` URI.
- **Automatic config-message deletion** on idle shutdown, cancellation, runner stop
  signals, and operational failures, plus an independent cleanup job that retries
  deletion from a persisted receipt.

## Set up

1. Create a Telegram bot with BotFather.
2. Create a **private** Telegram channel and add the bot as an administrator with
   **Post messages** permission. **Delete messages** is recommended for full cleanup;
   Telegram also lets a channel bot with Post messages delete its own outgoing
   messages. Anyone who can read the channel can use the shared config while the run
   is active.
3. In the GitHub repository, open **Settings → Secrets and variables → Actions**
   and add these **repository secrets**:

   | Secret | Value |
   | --- | --- |
   | `TG_BASE_URL` | `https://api.telegram.org`, or your custom Bot API root such as `https://telegram-proxy.example/prefix` |
   | `TG_BOT_TOKEN` | The bot token from BotFather |
   | `TG_CHANNEL_USERNAME` | `@your_channel`, or a negative channel ID such as `-1001234567890` |

   `TG_BASE_URL` must **not** contain the bot token, query parameters, or credentials.
   A trailing `/bot` is accepted and normalized. HTTPS is required except for a
   loopback Bot API server. The application sets both PTB URL parameters:

   ```python
   Bot(
       token=TG_BOT_TOKEN,
       base_url=f"{TG_BASE_URL}/bot",
       base_file_url=f"{TG_BASE_URL}/file/bot",
   )
   ```

   PTB appends the token to those URL prefixes itself. A custom Bot API server or
   reverse proxy must support `sendMessage`, `deleteMessage`, and preferably
   `deleteMessages`.

4. Put `.github/workflows/vpn.yml` on the repository's default branch so GitHub
   shows it in the Actions menu.

## Start, connect, and stop

1. Open **Actions → Ephemeral VPN → Run workflow**. There are no inputs.
2. Wait for the single Telegram message. It is one config-only `vless://` link.
3. On each device, open V2RayNG and use **+ → Import from clipboard** with that same
   link. Select the imported profile and connect. Multiple devices can use it at the
   same time.
4. Profiles use port **443**, TLS certificate verification, and the tunnel hostname
   for SNI/Host.
5. To stop manually, open the running workflow and select **Cancel run**.

The foreground Python supervisor keeps the job alive. It stops when:

- **5 minutes** pass with neither VPN traffic nor an active VPN stream;
- the run is cancelled or the runner sends a stop signal;
- GitHub ends the job; or
- Xray, the tunnel, or health monitoring fails.

A connected but quiet client counts as active. Traffic is measured using Xray's
shared-account counters; tunnel keepalives, Telegram requests, and metrics do not
reset the idle timer. Checks run every 5 seconds, so idle shutdown occurs on the first
check at or after the deadline. Eight consecutive failed health checks also stop the
server instead of leaving an unmonitored session running.

Every run generates a new UUID, a new secret path, and a new tunnel hostname. Old
profiles stop working when the run ends; delete them and import the new profile after
a restart.

## Config-message deletion

The bot records the delivered message ID in a non-secret receipt file. On normal
shutdown it deletes the message immediately. The workflow also uploads that receipt
as a short-lived artifact, and a separate `cleanup` job runs with `if: always()` to
retry deletion if the VPN job was cancelled or failed before it could finish cleanup.

Deletion is **best effort**, not an absolute guarantee:

- GitHub’s force-cancel path or abrupt runner loss can skip `always()` steps and
  artifact upload, leaving no cleanup opportunity.
- Telegram must still be reachable and the bot must retain delete permission.
- The receipt artifact contains only chat/message identifiers, not VPN credentials,
  and is retained for one day.

If a config message survives a failed cleanup, delete it manually in Telegram and
start a new run; the old credential is invalid once the runner exits.

## Speed and blocking resistance

The restored WebSocket transport uses the same link shape as the previously working
version, while keeping safe server-side tuning:

- `cloudflared --protocol http2` for the tunnel edge connection;
- 1 MiB Xray per-connection buffers;
- TCP fast-open, no-delay, and keepalive where supported;
- random secret paths and neutral profile remarks;
- TLS on port 443 with the Cloudflare hostname for SNI/Host.

These choices make fingerprinting and casual blocking harder, but they do **not**
make the service unblockable. `trycloudflare.com` quick-tunnel hostnames can still be
blocked as a whole, and TLS terminates at Cloudflare. REALITY/Vision cannot be used
through this TLS-terminating quick-tunnel architecture.

## Important limits

- GitHub-hosted jobs have a **six-hour execution limit**; the workflow explicitly
  sets `timeout-minutes: 360`. This is not an always-on VPS and cannot run indefinitely.
- Quick tunnels are best-effort, temporary infrastructure. Cloudflare or network
  restrictions may prevent connections; the workflow fails rather than publishing
  an unusable configuration if public readiness or end-to-end verification cannot be
  completed.
- The exit location is the runner's outbound network, not the Cloudflare edge.
  There is no static IP, guaranteed bandwidth, or availability guarantee.
- Use this only for lawful traffic and in accordance with GitHub's and Cloudflare's
  acceptable-use terms. Do not use it to evade platform limits or as a permanent
  proxy service. For reliable, long-running VPN hosting, use a VPS you control.
- A public channel exposes the shared credential to all readers. Prefer a private
  channel. Because all users share one UUID, you cannot revoke or meter one person
  independently without ending the run and issuing a new config.

## Security and lifecycle

- Only `workflow_dispatch` starts the VPN. There is no pull-request trigger.
- Workflow permissions are read-only; checkout does not persist credentials.
- Actions and Python dependencies are pinned; release binaries have fixed SHA-256
  checksums and are verified before installation.
- Xray, its statistics API, and cloudflared metrics bind to loopback. Only the
  outbound Cloudflare tunnel exposes the VPN endpoint.
- TLS terminates at Cloudflare. The origin leg travels through cloudflared's
  outbound connection; the client must trust Cloudflare with this proxy traffic.
- Private, loopback, link-local, and reserved destinations are blocked, including
  checks on resolved/connected addresses. This is defense in depth, not a substitute
  for account and channel security.
- The configuration is written outside the checkout with mode `0600`. Children
  receive an environment without Telegram or GitHub tokens. Sensitive process logs
  are not printed or uploaded, and temporary files are removed on graceful exit.
- SIGINT/SIGTERM trigger child-process termination and config-message deletion,
  with forced kill after a short grace period. A forced runner shutdown can skip
  cleanup, but the VM and session credentials are discarded.

## Repository layout

```text
.github/workflows/vpn.yml   Manual GitHub Actions entry point and cleanup job
vpn_server/config.py        Secret validation, shared Xray config, share links
vpn_server/install.py       Pinned binary downloads and checksum verification
vpn_server/runtime.py       Process management, readiness, activity monitoring
vpn_server/telegram.py      PTB config-only delivery, deletion, receipts
vpn_server/cleanup.py       Independent Telegram message cleanup
vpn_server/__main__.py      Foreground supervisor and graceful cleanup
tests/                      Offline unit and mocked integration tests
```

## Development and checks

Python 3.11+ is supported; the workflow uses Python 3.12. The installer targets
Linux x86-64, matching `ubuntu-24.04` in the workflow.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
ruff check vpn_server tests
ruff format --check vpn_server tests
```

The tests do not need real Telegram credentials and do not contact Telegram,
Cloudflare, or GitHub. They cover custom PTB URLs, config-only delivery, deletion
receipts, the shared WebSocket link format, end-to-end verification wiring, the idle
deadline, process cleanup, signal handling, checksum rejection, and workflow
restrictions. A real end-to-end run still needs valid repository secrets and GitHub
runner connectivity.

To regenerate the hash-pinned lockfiles after changing the `.in` files:

```bash
python -m pip install pip-tools==7.5.3
PIP_ONLY_BINARY=':all:' pip-compile --generate-hashes --no-header \
  --output-file requirements.txt requirements.in
PIP_ONLY_BINARY=':all:' pip-compile --generate-hashes --no-header \
  --output-file requirements-dev.txt requirements-dev.in
```

## Troubleshooting

- **Missing secrets:** add all three names exactly as shown under Actions secrets.
- **Telegram permission error:** confirm the bot is a channel administrator allowed
  to post and delete messages, and the channel username/ID belongs to that channel.
- **Custom API error:** check the API root and proxy prefix; do not append the token.
- **Legacy proxy `TypeError`:** channel preflight reads `getChat`/`getChatMember` as
  raw JSON so older proxies that omit newer PTB model fields can still work. If
  delivery still reports a response that PTB could not decode, update the proxy or
  use `https://api.telegram.org`. Unexpected-error logs include module/function/line
  locations, but not exception text, request URLs, or local values.
- **Tunnel startup timeout:** retry later and check whether the runner/network can
  reach Cloudflare. No Cloudflare account token is needed for this quick-tunnel mode.
- **End-to-end verification failed:** the workflow stops without publishing a config.
  Retry later; the temporary tunnel or outbound network may be unavailable.
- **Configuration delivery fails:** the job stops its children and exits unsuccessfully;
  it does not deliberately leave a VPN running without a delivered configuration.
- **Old profile no longer connects:** start a new run and import its new configuration.
