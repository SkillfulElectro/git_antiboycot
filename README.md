# git_antiboycot

An **on-demand, ephemeral VPN proxy** running in a GitHub Actions job. It starts
[Xray](https://github.com/XTLS/Xray-core) with **VLESS + WebSocket**, exposes it through
a [Cloudflare quick tunnel](https://github.com/cloudflare/cloudflared) over **TLS**, and
sends a V2RayNG-importable link and QR code to a Telegram channel using
[python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot).

No Cloudflare account, domain, certificate, or open inbound runner port is required.
This is a proxy for apps routed by V2RayNG, not a WireGuard/OpenVPN server. UDP can
be carried inside VLESS/WebSocket; this is not a raw UDP tunnel.

## Set up

1. Create a Telegram bot with BotFather.
2. Create a **private** Telegram channel and add the bot as an administrator with
   **Post messages** permission. Anyone who can read the channel can use the VPN
   while it is running.
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
   reverse proxy must support both API methods and file uploads.

4. Put `.github/workflows/vpn.yml` on the repository's default branch so GitHub
   shows it in the Actions menu.

## Start, connect, and stop

1. Open **Actions → Ephemeral VPN → Run workflow**.
2. Wait for the Telegram message. In V2RayNG, use **+ → Import from clipboard**
   with the supplied `vless://` link, or **+ → Scan QR code** with the PNG.
3. Select the imported profile and connect. The profile uses port **443**, TLS
   certificate verification, and the tunnel hostname for both SNI and WebSocket Host.
4. To stop manually, open the running workflow and select **Cancel run**. The
   Telegram message includes a link to that run.

The foreground Python supervisor keeps the job alive. It stops when:

- **60 minutes** pass with neither VPN traffic nor an active VPN stream;
- the run is cancelled or the runner sends a stop signal;
- GitHub ends the job; or
- Xray, the tunnel, or health monitoring fails.

A connected but quiet client counts as active. Traffic is measured using Xray's
per-user counters; tunnel keepalives, Telegram requests, and metrics do not reset
the idle timer. Checks run every 15 seconds, so idle shutdown occurs on the first
check at or after the deadline. Eight consecutive failed health checks also stop
the server instead of leaving an unmonitored session running.

Every run generates a new UUID, WebSocket path, and tunnel hostname. Old profiles
stop working when the run ends; delete them and import the new profile after a restart.
A stopped notification is best effort, not a guarantee during forced cancellation.

## Important limits

- GitHub-hosted jobs have a **six-hour execution limit**; the workflow explicitly
  sets `timeout-minutes: 360`. This is not an always-on VPS and cannot run indefinitely.
- Quick tunnels are best-effort, temporary infrastructure. Cloudflare or network
  restrictions may prevent connections; the workflow fails rather than publishing
  an unusable configuration if public readiness cannot be verified.
- The exit location is the runner's outbound network, not the Cloudflare edge.
  There is no static IP, guaranteed bandwidth, or availability guarantee.
- Use this only for lawful traffic and in accordance with GitHub's and Cloudflare's
  acceptable-use terms. Do not use it to evade platform limits or as a permanent
  proxy service. For reliable, long-running VPN hosting, use a VPS you control.
- A public channel exposes the credential to all readers. Prefer a private channel.
  Telegram retains the delivered message; the credential expires with the runner,
  but the message itself is not automatically deleted.

## Security and lifecycle

- Only `workflow_dispatch` starts the VPN. There is no pull-request trigger.
- Workflow permissions are read-only; checkout does not persist credentials.
- Actions and Python dependencies are pinned; release binaries have fixed SHA-256
  checksums and are verified before installation.
- Xray, its statistics API, and cloudflared metrics bind to loopback. Only the
  outbound Cloudflare tunnel exposes the VPN's WebSocket endpoint.
- TLS terminates at Cloudflare. The origin leg travels through cloudflared's
  outbound connection; the client must trust Cloudflare with this proxy traffic.
- Private, loopback, link-local, and reserved destinations are blocked, including
  checks on resolved/connected addresses. This is defense in depth, not a substitute
  for account and channel security.
- The configuration is written outside the checkout with mode `0600`. Children
  receive an environment without Telegram or GitHub tokens. Sensitive process logs
  are not printed or uploaded, and temporary files are removed on graceful exit.
- SIGINT/SIGTERM trigger child-process termination, with forced kill after a short
  grace period. A forced runner shutdown can skip cleanup, but the VM and session
  credentials are discarded.

## Repository layout

```text
.github/workflows/vpn.yml   Manual GitHub Actions entry point
vpn_server/config.py       Secret validation, Xray config, V2RayNG share link
vpn_server/install.py      Pinned binary downloads and checksum verification
vpn_server/runtime.py      Process management, readiness, activity monitoring
vpn_server/telegram.py     PTB delivery, permissions, retries, QR generation
vpn_server/__main__.py     Foreground supervisor and cleanup
tests/                     Offline unit and mocked integration tests
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
Cloudflare, or GitHub. They cover custom PTB URLs, delivery and retries, share-link
encoding, the idle deadline, process cleanup, signal handling, checksum rejection,
and workflow restrictions. A real end-to-end run still needs valid repository
secrets and GitHub runner connectivity.

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
  to post, and the channel username/ID belongs to that channel.
- **Custom API error:** check the API root and proxy prefix; do not append the token.
- **Tunnel startup timeout:** retry later and check whether the runner/network can
  reach Cloudflare. No Cloudflare account token is needed for this quick-tunnel mode.
- **Configuration delivery fails:** the job stops its children and exits unsuccessfully;
  it does not deliberately leave a VPN running without a delivered configuration.
- **Old profile no longer connects:** start a new run and import its new configuration.
