import ipaddress
from urllib.parse import parse_qs, urlsplit

import pytest

from vpn_server.config import (
    BLOCKED_IPS,
    IDLE_TIMEOUT_SECONDS,
    STATS_PREFIX,
    USER_EMAIL,
    ConfigurationError,
    TelegramSettings,
    VpnConfig,
    normalize_base_url,
    run_url,
    share_link,
    validate_channel,
    validate_tunnel_host,
    xray_client_config,
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


def test_idle_timeout_is_five_minutes():
    assert IDLE_TIMEOUT_SECONDS == 300


def test_xray_config_has_one_shared_account_and_loopback_only_listeners():
    config = VpnConfig.create((21001, 21002, 21003))
    data = xray_config(config)
    assert all(inbound["listen"] == "127.0.0.1" for inbound in data["inbounds"])
    vpn, api = data["inbounds"]
    assert vpn["protocol"] == "vless"
    clients = vpn["settings"]["clients"]
    assert clients == [{"id": config.client_id, "email": USER_EMAIL, "level": 0}]
    assert vpn["settings"]["decryption"] == "none"
    assert api["protocol"] == "dokodemo-door"
    assert data["api"]["services"] == ["StatsService"]
    assert data["routing"]["rules"][0]["outboundTag"] == "api"
    assert data["policy"]["levels"]["0"]["statsUserUplink"] is True
    assert data["policy"]["levels"]["0"]["statsUserDownlink"] is True
    assert data["policy"]["levels"]["0"]["connIdle"] == 300
    assert data["policy"]["levels"]["0"]["bufferSize"] == 1024
    assert config.client_id not in repr(config)
    assert config.ws_path not in repr(config)
    assert STATS_PREFIX == f"user>>>{USER_EMAIL}>>>traffic>>>"


def test_server_uses_previous_working_plain_websocket_path():
    config = VpnConfig.create((21001, 21002, 21003))
    stream = xray_config(config)["inbounds"][0]["streamSettings"]
    assert stream == {
        "network": "ws",
        "security": "none",
        "wsSettings": {"path": config.ws_path},
    }


def test_share_link_matches_previous_working_websocket_format():
    config = VpnConfig.create((21001, 21002, 21003))
    link = share_link(config, "test-tunnel.trycloudflare.com")
    parsed = urlsplit(link)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "vless"
    assert parsed.username == config.client_id
    assert parsed.hostname == "test-tunnel.trycloudflare.com"
    assert parsed.port == 443
    assert query == {
        "type": ["ws"],
        "security": ["tls"],
        "encryption": ["none"],
        "sni": [parsed.hostname],
        "host": [parsed.hostname],
        "path": [config.ws_path],
        "fp": ["chrome"],
        "alpn": ["http/1.1"],
    }
    assert "allowInsecure" not in query
    assert "ed" not in query
    assert "extra" not in query
    assert parsed.fragment.startswith("node-")
    assert "vpn" not in parsed.fragment.lower()
    assert "antiboycot" not in link.lower()


def test_probe_client_mirrors_the_published_link():
    config = VpnConfig.create((21001, 21002, 21003))
    host = "test-tunnel.trycloudflare.com"
    link = urlsplit(share_link(config, host))
    query = parse_qs(link.query)
    client = xray_client_config(config, host, 28080)
    inbound = client["inbounds"][0]
    assert inbound["listen"] == "127.0.0.1"
    assert inbound["port"] == 28080
    assert inbound["protocol"] == "http"
    outbound = client["outbounds"][0]
    assert outbound["protocol"] == "vless"
    server = outbound["settings"]["vnext"][0]
    assert server["address"] == link.hostname
    assert server["port"] == link.port
    assert server["users"] == [{"id": config.client_id, "encryption": "none", "level": 0}]
    stream = outbound["streamSettings"]
    assert stream["network"] == query["type"][0]
    assert stream["security"] == query["security"][0]
    assert stream["tlsSettings"]["serverName"] == query["sni"][0]
    assert stream["tlsSettings"]["allowInsecure"] is False
    assert stream["tlsSettings"]["fingerprint"] == query["fp"][0]
    assert stream["tlsSettings"]["alpn"] == query["alpn"]
    assert stream["wsSettings"]["path"] == query["path"][0]
    assert stream["wsSettings"]["headers"]["Host"] == query["host"][0]
    assert "mux" not in outbound


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"])
def test_private_addresses_blocked_after_dns_resolution(address):
    assert any(ipaddress.ip_address(address) in ipaddress.ip_network(cidr) for cidr in BLOCKED_IPS)
    data = xray_config(VpnConfig.create((21001, 21002, 21003)))
    freedom = data["outbounds"][0]
    assert freedom["settings"]["domainStrategy"] == "UseIP"
    assert freedom["settings"]["finalRules"][0]["action"] == "block"
    assert freedom["streamSettings"]["sockopt"]["tcpFastOpen"] is True
    assert freedom["streamSettings"]["sockopt"]["tcpNoDelay"] is True


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
    first = VpnConfig.create((21001, 21002, 21003))
    second = VpnConfig.create((21001, 21002, 21003))
    assert first.client_id != second.client_id
    assert first.ws_path != second.ws_path
    assert first.ws_path.startswith("/") and " " not in first.ws_path


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
