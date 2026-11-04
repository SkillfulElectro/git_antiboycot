import ipaddress
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from vpn_server.config import (
    BLOCKED_IPS,
    IDLE_TIMEOUT_SECONDS,
    MAX_USERS,
    ConfigurationError,
    TelegramSettings,
    VpnConfig,
    normalize_base_url,
    run_url,
    share_link,
    validate_channel,
    validate_transport,
    validate_tunnel_host,
    validate_users,
    xray_config,
)

TOKEN = "123456789:" + "A" * 35
ENV = {
    "TG_BASE_URL": "https://telegram.example/proxy/",
    "TG_BOT_TOKEN": TOKEN,
    "TG_CHANNEL_USERNAME": "@private_vpn",
}


def test_settings_use_custom_api_root():
    settings = TelegramSettings.from_env(ENV)
    assert settings.api_url == "https://telegram.example/proxy/bot"
    assert settings.file_url == "https://telegram.example/proxy/file/bot"
    assert settings.channel == "@private_vpn"
    assert TOKEN not in repr(settings)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://api.telegram.org", "https://api.telegram.org"),
        ("https://api.telegram.org/bot/", "https://api.telegram.org"),
        ("https://proxy.example/prefix///", "https://proxy.example/prefix"),
        ("http://127.0.0.1:8081/", "http://127.0.0.1:8081"),
        ("http://[::1]:8081", "http://[::1]:8081"),
    ],
)
def test_base_url_normalization(value, expected):
    assert normalize_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://proxy.example",
        "ftp://proxy.example",
        "https://user:password@proxy.example",
        "https://proxy.example/?token=secret",
        "https://proxy.example/#fragment",
        "https://proxy.example/bot" + TOKEN,
        "https://proxy.example/file/bot",
        "https://proxy.example:70000",
        "https://proxy.example:0",
        "https://proxy.example\\@evil.example",
        "https://proxy.example/\npath",
        "not a URL",
    ],
)
def test_reject_unsafe_base_urls(value):
    with pytest.raises(ConfigurationError) as error:
        normalize_base_url(value)
    assert TOKEN not in str(error.value)


def test_missing_secrets_names_only():
    with pytest.raises(ConfigurationError, match="TG_BOT_TOKEN, TG_CHANNEL_USERNAME") as error:
        TelegramSettings.from_env({"TG_BASE_URL": "https://api.telegram.org"})
    assert ENV["TG_BASE_URL"] not in str(error.value)


@pytest.mark.parametrize("channel", ["@valid_channel", "valid_channel", "-1001234567890"])
def test_valid_channels(channel):
    assert validate_channel(channel).startswith(("@", "-"))


@pytest.mark.parametrize("channel", ["https://t.me/name", "@bad-name", "12345", "@x", "-"])
def test_invalid_channels(channel):
    with pytest.raises(ConfigurationError):
        validate_channel(channel)


@pytest.mark.parametrize(("value", "expected"), [("1", 1), ("5", 5), (20, 20)])
def test_valid_user_counts(value, expected):
    assert validate_users(value) == expected


@pytest.mark.parametrize("value", ["0", "21", "-1", "abc", "1.5", True, None])
def test_invalid_user_counts(value):
    with pytest.raises(ConfigurationError):
        validate_users(value)


@pytest.mark.parametrize(("value", "expected"), [("xhttp", "xhttp"), ("WS", "ws")])
def test_valid_transports(value, expected):
    assert validate_transport(value) == expected


@pytest.mark.parametrize("value", ["grpc", "tcp", "", "reality"])
def test_invalid_transports(value):
    with pytest.raises(ConfigurationError):
        validate_transport(value)


def test_idle_timeout_is_five_minutes():
    assert IDLE_TIMEOUT_SECONDS == 300


def test_xray_config_is_loopback_only_multi_user_and_metered():
    config = VpnConfig.create((21001, 21002, 21003), users=3)
    data = xray_config(config)
    assert all(inbound["listen"] == "127.0.0.1" for inbound in data["inbounds"])
    vpn, api = data["inbounds"]
    assert vpn["protocol"] == "vless"
    clients = vpn["settings"]["clients"]
    assert len(clients) == 3
    assert len({client["id"] for client in clients}) == 3
    assert len({client["email"] for client in clients}) == 3
    assert vpn["streamSettings"]["network"] == "xhttp"
    assert vpn["streamSettings"]["xhttpSettings"]["path"] == config.path
    assert vpn["streamSettings"]["xhttpSettings"]["mode"] == "auto"
    assert api["protocol"] == "dokodemo-door"
    assert data["api"]["services"] == ["StatsService"]
    assert data["routing"]["rules"][0]["outboundTag"] == "api"
    assert data["policy"]["levels"]["0"]["statsUserUplink"] is True
    assert data["policy"]["levels"]["0"]["statsUserDownlink"] is True
    assert data["policy"]["levels"]["0"]["connIdle"] == 300
    assert data["policy"]["levels"]["0"]["bufferSize"] == 1024
    assert config.client_id not in repr(config)
    assert config.path not in repr(config)
    assert len(config.stats_prefixes) == 3


def test_ws_fallback_uses_early_data():
    config = VpnConfig.create((21001, 21002, 21003), users=1, transport="ws")
    data = xray_config(config)
    stream = data["inbounds"][0]["streamSettings"]
    assert stream["network"] == "ws"
    assert stream["wsSettings"]["maxEarlyData"] == 2048
    link = share_link(config, "test-tunnel.trycloudflare.com")
    query = parse_qs(urlsplit(link).query)
    assert query["path"] == [config.path + "?ed=2048"]


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"])
def test_private_addresses_blocked_after_dns_resolution(address):
    assert any(ipaddress.ip_address(address) in ipaddress.ip_network(cidr) for cidr in BLOCKED_IPS)
    data = xray_config(VpnConfig.create((21001, 21002, 21003)))
    freedom = data["outbounds"][0]
    assert freedom["settings"]["domainStrategy"] == "UseIP"
    assert freedom["settings"]["finalRules"][0]["action"] == "block"
    assert freedom["streamSettings"]["sockopt"]["tcpFastOpen"] is True
    assert freedom["streamSettings"]["sockopt"]["tcpNoDelay"] is True


def test_xhttp_share_link_is_v2rayng_compatible_and_neutral():
    config = VpnConfig.create((21001, 21002, 21003), users=2)
    link = share_link(config, "test-tunnel.trycloudflare.com", index=1)
    parsed = urlsplit(link)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "vless"
    assert parsed.username == config.clients[1].client_id
    assert parsed.hostname == "test-tunnel.trycloudflare.com"
    assert parsed.port == 443
    assert query["type"] == ["xhttp"]
    assert query["security"] == ["tls"]
    assert query["encryption"] == ["none"]
    assert query["sni"] == [parsed.hostname]
    assert query["host"] == [parsed.hostname]
    assert query["path"] == [config.path]
    assert query["mode"] == ["packet-up"]
    assert query["alpn"] == ["h2"]
    assert query["fp"] == ["chrome"]
    assert "allowInsecure" not in query
    extra = json.loads(query["extra"][0])
    assert extra["xmux"]["maxConcurrency"] == "16-32"
    assert extra["xPaddingBytes"] == "100-1000"
    assert parsed.fragment.startswith("node-")
    assert "vpn" not in parsed.fragment.lower()
    assert "antiboycot" not in link.lower()


def test_share_link_index_bounds():
    config = VpnConfig.create((21001, 21002, 21003), users=1)
    with pytest.raises(ConfigurationError):
        share_link(config, "test-tunnel.trycloudflare.com", index=1)


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "a.trycloudflare.com.evil.test",
        "-a.trycloudflare.com",
        "a.trycloudflare.com\n",
    ],
)
def test_reject_untrusted_tunnel_host(host):
    with pytest.raises(ConfigurationError):
        validate_tunnel_host(host)


def test_each_session_gets_new_credentials():
    first = VpnConfig.create((21001, 21002, 21003), users=MAX_USERS)
    second = VpnConfig.create((21001, 21002, 21003), users=MAX_USERS)
    assert first.client_id != second.client_id
    assert first.path != second.path
    assert first.path.startswith("/") and " " not in first.path
    assert len({client.client_id for client in first.clients}) == MAX_USERS


def test_run_url():
    assert (
        run_url(
            {
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_REPOSITORY": "owner/repo",
                "GITHUB_RUN_ID": "123",
            }
        )
        == "https://github.com/owner/repo/actions/runs/123"
    )
