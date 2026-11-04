"""Configuration and V2RayNG share-link construction; no network or disk access."""

from __future__ import annotations

import json
import os
import re
import secrets
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

IDLE_TIMEOUT_SECONDS = 5 * 60
POLL_SECONDS = 5
STARTUP_TIMEOUT_SECONDS = 150
DEFAULT_USERS = 5
MAX_USERS = 20
TRANSPORTS = ("xhttp", "ws")
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


def validate_users(value: str | int) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        users = int(value)
    except (TypeError, ValueError):
        raise ConfigurationError("VPN_USERS must be an integer from 1 to 20.") from None
    if not 1 <= users <= MAX_USERS:
        raise ConfigurationError("VPN_USERS must be an integer from 1 to 20.")
    return users


def validate_transport(value: str) -> str:
    value = value.strip().lower()
    if value not in TRANSPORTS:
        raise ConfigurationError("VPN_TRANSPORT must be xhttp or ws.")
    return value


@dataclass(frozen=True)
class Client:
    client_id: str = field(repr=False)
    email: str

    @property
    def stats_prefix(self) -> str:
        return f"user>>>{self.email}>>>traffic>>>"


@dataclass(frozen=True)
class VpnConfig:
    clients: tuple[Client, ...]
    path: str = field(repr=False)
    origin_port: int
    api_port: int
    metrics_port: int
    transport: str = "xhttp"

    @classmethod
    def create(
        cls, ports: tuple[int, int, int], users: int = DEFAULT_USERS, transport: str = "xhttp"
    ) -> VpnConfig:
        users = validate_users(users)
        transport = validate_transport(transport)
        if len(set(ports)) != 3 or any(not 1 <= port <= 65535 for port in ports):
            raise ConfigurationError("Three distinct, valid loopback ports are required.")
        clients = tuple(Client(str(uuid.uuid4()), f"u{secrets.token_hex(8)}") for _ in range(users))
        path = "/" + secrets.token_urlsafe(18)
        return cls(clients, path, *ports, transport)

    @property
    def client_id(self) -> str:
        return self.clients[0].client_id

    @property
    def ws_path(self) -> str:
        return self.path

    @property
    def stats_prefixes(self) -> frozenset[str]:
        return frozenset(client.stats_prefix for client in self.clients)


def xray_config(config: VpnConfig) -> dict:
    """Build a multi-user VLESS origin and a separate loopback-only stats API."""
    if config.transport == "xhttp":
        stream = {
            "network": "xhttp",
            "security": "none",
            "xhttpSettings": {
                "path": config.path,
                "mode": "auto",
                "xPaddingBytes": "100-1000",
                "scMaxEachPostBytes": 1000000,
                "scMaxBufferedPosts": 30,
            },
        }
    else:
        stream = {
            "network": "ws",
            "security": "none",
            "wsSettings": {"path": config.path, "maxEarlyData": 2048},
        }
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
                    "clients": [
                        {"id": client.client_id, "email": client.email, "level": 0}
                        for client in config.clients
                    ],
                    "decryption": "none",
                },
                "streamSettings": stream,
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


def validate_tunnel_host(host: str) -> str:
    if not TUNNEL_HOST_RE.fullmatch(host):
        raise ConfigurationError("Cloudflare returned an invalid quick-tunnel hostname.")
    return host


def client_extra(transport: str) -> dict:
    if transport == "xhttp":
        return {
            "xPaddingBytes": "100-1000",
            "scMaxEachPostBytes": 1000000,
            "scMinPostsIntervalMs": "10-30",
            "xmux": {
                "maxConcurrency": "16-32",
                "hMaxRequestTimes": "600-900",
                "hMaxReusableSecs": "1800-3000",
            },
        }
    return {}


def share_link(config: VpnConfig, host: str, index: int = 0, label: str | None = None) -> str:
    host = validate_tunnel_host(host)
    if not 0 <= index < len(config.clients):
        raise ConfigurationError("Client index is out of range.")
    client = config.clients[index]
    if config.transport == "xhttp":
        query = {
            "type": "xhttp",
            "security": "tls",
            "encryption": "none",
            "sni": host,
            "host": host,
            "path": config.path,
            "mode": "packet-up",
            "fp": "chrome",
            "alpn": "h2",
            "extra": json.dumps(client_extra("xhttp"), separators=(",", ":")),
        }
    else:
        query = {
            "type": "ws",
            "security": "tls",
            "encryption": "none",
            "sni": host,
            "host": host,
            "path": config.path + "?ed=2048",
            "fp": "chrome",
            "alpn": "http/1.1",
        }
    encoded = urlencode(query, quote_via=quote)
    # Neutral, non-identifying remark. The URI itself still necessarily reveals
    # the transport/security fields required by the client.
    fragment = quote(label if label is not None else f"node-{secrets.token_hex(3)}", safe="")
    return f"vless://{client.client_id}@{host}:443?{encoded}#{fragment}"


def run_url(env: dict[str, str]) -> str:
    server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repo = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) and run_id.isdigit():
        return f"{server}/{repo}/actions/runs/{run_id}"
    return server
