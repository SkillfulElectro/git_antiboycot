"""Configuration and V2RayNG share-link construction; no network or disk access."""

from __future__ import annotations

import os
import re
import secrets
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

IDLE_TIMEOUT_SECONDS = 5 * 60
POLL_SECONDS = 5
STARTUP_TIMEOUT_SECONDS = 150
USER_EMAIL = "shared"
STATS_PREFIX = f"user>>>{USER_EMAIL}>>>traffic>>>"
TUNNEL_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com\Z")

# Explicit rules avoid a geoip.dat dependency. Freedom's final rules also check
# the resolved and actually connected address, rather than trusting DNS alone.
BLOCKED_IPS = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "::/128",
    "::1/128",
    "64:ff9b::/96",
    "2001:db8::/32",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)


class ConfigurationError(ValueError):
    """An actionable configuration error that contains no secret values."""


@dataclass(frozen=True)
class TelegramSettings:
    base_url: str = field(repr=False)
    token: str = field(repr=False)
    channel: str = field(repr=False)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TelegramSettings:
        env = os.environ if env is None else env
        values = {
            name: env.get(name, "").strip()
            for name in ("TG_BASE_URL", "TG_BOT_TOKEN", "TG_CHANNEL_USERNAME")
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ConfigurationError("Missing GitHub secrets: " + ", ".join(missing))
        if any(ord(c) < 32 or ord(c) == 127 for value in values.values() for c in value):
            raise ConfigurationError("Telegram secrets must not contain control characters.")
        return cls(
            normalize_base_url(values["TG_BASE_URL"]),
            validate_token(values["TG_BOT_TOKEN"]),
            validate_channel(values["TG_CHANNEL_USERNAME"]),
        )

    @property
    def api_url(self) -> str:
        return self.base_url + "/bot"

    @property
    def file_url(self) -> str:
        return self.base_url + "/file/bot"


def normalize_base_url(value: str) -> str:
    """Accept an API root, optionally with a proxy prefix or a trailing /bot."""
    try:
        parsed = urlsplit(value)
        port = parsed.port  # Validate even when the port is not used separately.
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or any(c.isspace() for c in value)
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError
    except ValueError:
        raise ConfigurationError(
            "TG_BASE_URL must be an HTTP(S) API root without credentials, query, or fragment."
        ) from None
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ConfigurationError("TG_BASE_URL requires HTTPS (HTTP is allowed only on loopback).")
    path = parsed.path.rstrip("/")
    if re.search(r"/bot\d+:[^/]+(?:/|$)", path) or path.endswith("/file/bot"):
        raise ConfigurationError("Put the token in TG_BOT_TOKEN, not in TG_BASE_URL.")
    if path.endswith("/bot"):
        path = path[:-4]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def validate_token(value: str) -> str:
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", value):
        raise ConfigurationError("TG_BOT_TOKEN is not a valid Telegram bot token.")
    return value


def validate_channel(value: str) -> str:
    if re.fullmatch(r"-?[0-9]+", value):
        if not value.startswith("-"):
            raise ConfigurationError("A numeric channel ID must start with '-'.")
        return value
    channel = value if value.startswith("@") else "@" + value
    if not re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", channel):
        raise ConfigurationError("TG_CHANNEL_USERNAME must be @channelname or a negative chat ID.")
    return channel


@dataclass(frozen=True)
class VpnConfig:
    client_id: str = field(repr=False)
    ws_path: str = field(repr=False)
    origin_port: int
    api_port: int
    metrics_port: int

    @classmethod
    def create(cls, ports: tuple[int, int, int]) -> VpnConfig:
        if len(set(ports)) != 3 or any(not 1 <= port <= 65535 for port in ports):
            raise ConfigurationError("Three distinct, valid loopback ports are required.")
        return cls(str(uuid.uuid4()), "/" + secrets.token_urlsafe(24), *ports)


def xray_config(config: VpnConfig) -> dict:
    """Build one shared VLESS/WebSocket account and a loopback-only stats API.

    VLESS does not limit a UUID to one device. Every device that imports the
    same link authenticates as this one account and can connect simultaneously.
    """
    return {
        "log": {"loglevel": "warning"},
        "stats": {},
        "api": {"tag": "api", "services": ["StatsService"]},
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True,
                    "handshake": 4,
                    "connIdle": 300,
                    "uplinkOnly": 0,
                    "downlinkOnly": 0,
                    "bufferSize": 1024,
                }
            }
        },
        "inbounds": [
            {
                "tag": "vpn",
                "listen": "127.0.0.1",
                "port": config.origin_port,
                "protocol": "vless",
                "settings": {
                    "clients": [{"id": config.client_id, "email": USER_EMAIL, "level": 0}],
                    "decryption": "none",
                },
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {"path": config.ws_path},
                },
                "sniffing": {"enabled": False},
            },
            {
                "tag": "api-in",
                "listen": "127.0.0.1",
                "port": config.api_port,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
            },
        ],
        "outbounds": [
            {
                "tag": "direct",
                "protocol": "freedom",
                "settings": {
                    "domainStrategy": "UseIP",
                    "finalRules": [{"action": "block", "ip": list(BLOCKED_IPS)}],
                },
                "streamSettings": {
                    "sockopt": {"tcpFastOpen": True, "tcpNoDelay": True, "tcpKeepAliveInterval": 30}
                },
            },
            {"tag": "blocked", "protocol": "blackhole"},
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {"type": "field", "inboundTag": ["api-in"], "outboundTag": "api"},
                {
                    "type": "field",
                    "inboundTag": ["vpn"],
                    "domain": [
                        "full:localhost",
                        "domain:localhost",
                        "full:metadata.google.internal",
                    ],
                    "outboundTag": "blocked",
                },
                {
                    "type": "field",
                    "inboundTag": ["vpn"],
                    "ip": list(BLOCKED_IPS),
                    "outboundTag": "blocked",
                },
            ],
        },
    }


def xray_client_config(config: VpnConfig, host: str, proxy_port: int) -> dict:
    """Build a temporary local client used to verify the published link settings.

    The outbound mirrors the share link exactly. Mux is left disabled so two
    concurrent probe requests exercise two independent VLESS sessions using the
    same shared UUID.
    """
    host = validate_tunnel_host(host)
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "tag": "probe-http",
                "listen": "127.0.0.1",
                "port": proxy_port,
                "protocol": "http",
                "settings": {"allowTransparent": False},
                "sniffing": {"enabled": False},
            }
        ],
        "outbounds": [
            {
                "tag": "proxy",
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": host,
                            "port": 443,
                            "users": [{"id": config.client_id, "encryption": "none", "level": 0}],
                        }
                    ]
                },
                "streamSettings": {
                    "network": "ws",
                    "security": "tls",
                    "tlsSettings": {
                        "serverName": host,
                        "allowInsecure": False,
                        "fingerprint": "chrome",
                        "alpn": ["http/1.1"],
                    },
                    "wsSettings": {"path": config.ws_path, "headers": {"Host": host}},
                },
            }
        ],
    }


def validate_tunnel_host(host: str) -> str:
    if not TUNNEL_HOST_RE.fullmatch(host):
        raise ConfigurationError("Cloudflare returned an invalid quick-tunnel hostname.")
    return host


def share_link(config: VpnConfig, host: str, label: str | None = None) -> str:
    host = validate_tunnel_host(host)
    query = urlencode(
        {
            "type": "ws",
            "security": "tls",
            "encryption": "none",
            "sni": host,
            "host": host,
            "path": config.ws_path,
            "fp": "chrome",
            "alpn": "http/1.1",
        },
        quote_via=quote,
    )
    # Neutral, non-identifying remark. The URI itself still necessarily reveals
    # the transport/security fields required by the client.
    fragment = quote(label if label is not None else f"node-{secrets.token_hex(3)}", safe="")
    return f"vless://{config.client_id}@{host}:443?{query}#{fragment}"


def run_url(env: dict[str, str]) -> str:
    server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repo = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) and run_id.isdigit():
        return f"{server}/{repo}/actions/runs/{run_id}"
    return server
