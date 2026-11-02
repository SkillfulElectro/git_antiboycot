import hashlib
import io
import zipfile
from unittest.mock import MagicMock

import pytest

from vpn_server.install import download, install, write_executable


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def test_verified_download(monkeypatch, tmp_path):
    data = b"verified binary"
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: Response(data))
    assert (
        download("https://github.com/fixed/release", hashlib.sha256(data).hexdigest(), tmp_path)
        == data
    )
    assert list(tmp_path.iterdir()) == []


def test_checksum_mismatch_never_installs(monkeypatch, tmp_path):
    opened = MagicMock()
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: Response(b"tampered"))
    monkeypatch.setattr("vpn_server.install.urllib.request.urlopen", opened)
    opened.return_value = Response(b"tampered")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        download("https://github.com/fixed/release", "0" * 64, tmp_path)
    assert opened.call_count == 1
    assert list(tmp_path.iterdir()) == []


def test_network_retry_is_bounded(monkeypatch, tmp_path):
    opened = MagicMock(side_effect=OSError("offline"))
    monkeypatch.setattr("vpn_server.install.urllib.request.urlopen", opened)
    with pytest.raises(RuntimeError, match="verified release"):
        download("https://github.com/fixed/release", "0" * 64, tmp_path)
    assert opened.call_count == 3


def test_only_xray_binary_extracted(monkeypatch, tmp_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("xray", b"xray")
        bundle.writestr("../../escape", b"must not extract")
    downloads = {"Xray-linux-64.zip": archive.getvalue(), "cloudflared-linux-amd64": b"cloudflared"}
    monkeypatch.setattr(
        "vpn_server.install.download",
        lambda url, checksum, directory: downloads[url.rsplit("/", 1)[-1]],
    )
    install(tmp_path)
    assert {path.name for path in tmp_path.iterdir()} == {"xray", "cloudflared"}
    assert (tmp_path / "xray").read_bytes() == b"xray"
    assert not (tmp_path.parent.parent / "escape").exists()


def test_executable_is_private(tmp_path):
    path = tmp_path / "binary"
    write_executable(path, b"data")
    assert path.stat().st_mode & 0o777 == 0o700
    assert not path.with_suffix(".part").exists()
