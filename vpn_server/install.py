"""Download pinned, checksum-verified Linux x86-64 server binaries."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import tempfile
import urllib.request
import zipfile
from pathlib import Path

XRAY_VERSION = "v26.10.31"
XRAY_SHA256 = "e4bd9119f50ae7c41658b1abd0679ae06f98bd864a2907ff33f2f2cf7081b214"
CLOUDFLARED_VERSION = "2026.10.7"
CLOUDFLARED_SHA256 = "1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872"
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


def download(url: str, expected_sha256: str, directory: Path) -> bytes:
    """Never write an executable until its complete release checksum matches."""
    last_error: Exception | None = None
    for _attempt in range(3):
        temporary: Path | None = None
        try:
            digest = hashlib.sha256()
            size = 0
            request = urllib.request.Request(  # noqa: S310 - fixed HTTPS release URLs
                url, headers={"User-Agent": "git-antiboycot"}
            )
            with urllib.request.urlopen(request, timeout=90) as response:  # noqa: S310
                with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
                    temporary = Path(output.name)
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ValueError("Release asset exceeds the download size limit.")
                        digest.update(chunk)
                        output.write(chunk)
            if digest.hexdigest() != expected_sha256:
                raise ValueError("Release SHA-256 mismatch; refusing to use the download.")
            return temporary.read_bytes()
        except (OSError, ValueError) as exc:
            last_error = exc
            if isinstance(exc, ValueError):
                break
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    raise RuntimeError(f"Could not download a verified release: {last_error}") from None


def write_executable(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(".part")
    with temporary.open("wb") as output:
        output.write(content)
    temporary.chmod(0o700)
    temporary.replace(path)


def install(directory: Path) -> None:
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    base = "https://github.com/XTLS/Xray-core/releases/download"
    print(f"Installing Xray {XRAY_VERSION} (Linux x86-64).")
    archive = download(f"{base}/{XRAY_VERSION}/Xray-linux-64.zip", XRAY_SHA256, directory)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        # Extract only the binary, never archive-controlled paths.
        write_executable(directory / "xray", bundle.read("xray"))
    base = "https://github.com/cloudflare/cloudflared/releases/download"
    print(f"Installing cloudflared {CLOUDFLARED_VERSION} (Linux x86-64).")
    binary = download(
        f"{base}/{CLOUDFLARED_VERSION}/cloudflared-linux-amd64",
        CLOUDFLARED_SHA256,
        directory,
    )
    write_executable(directory / "cloudflared", binary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    install(args.directory)
    print("Both release checksums verified.")


if __name__ == "__main__":
    main()
