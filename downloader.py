from __future__ import annotations

import json
import random
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote, urlparse

import requests
from tqdm import tqdm

CK_RESOLVE_URL = (
    "https://ckdatabasews.icloud.com/database/1/"
    "com.apple.cloudkit/production/public/records/resolve"
)

USER_AGENT_TEMPLATES = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/118.0",
]


class ShareResolveError(RuntimeError):
    """Raised when the CloudKit resolve endpoint returns an error."""


def extract_short_guid(share_url: str) -> str:
    """
    Extract the short GUID from a public iCloud Drive URL.

    Expected formats (examples):
        https://www.icloud.com/iclouddrive/<short_guid>
        https://www.icloud.com/iclouddrive/<short_guid>#<name>
    """
    parsed = urlparse(share_url)
    parts = [segment for segment in parsed.path.split("/") if segment]
    try:
        idx = parts.index("iclouddrive")
    except ValueError as exc:
        raise ValueError("URL does not look like an iCloud Drive share link") from exc

    if idx + 1 >= len(parts):
        raise ValueError("Share link is missing the short GUID segment")
    return parts[idx + 1]


def extract_user_filename(share_url: str) -> Optional[str]:
    """Return the URL fragment (after '#') if present."""
    parsed = urlparse(share_url)
    fragment = parsed.fragment.strip()
    return fragment or None


def resolve_share(short_guid: str, session: Optional[requests.Session] = None) -> Dict:
    """Call the public CloudKit resolve endpoint for the given short GUID."""
    payload = {"shortGUIDs": [{"value": short_guid}]}
    close_after = session is None
    sess = session or create_session()
    try:
        response = sess.post(CK_RESOLVE_URL, json=payload)
        response.raise_for_status()
        data = response.json()

        results = data.get("results", [])
        if not results:
            raise ShareResolveError("No results returned by CloudKit resolve endpoint")

        result = results[0]
        if "serverErrorCode" in result:
            code = result.get("serverErrorCode")
            reason = result.get("reason", "Unknown error")
            raise ShareResolveError(f"CloudKit error {code}: {reason}")

        return result
    finally:
        if close_after:
            sess.close()


def build_download_metadata(resolved: Dict, share_url: str) -> Dict[str, Optional[str]]:
    """Extract the direct download URL and metadata from the resolved record."""
    share_fields = resolved.get("share", {}).get("fields", {})
    root_fields = resolved.get("rootRecord", {}).get("fields", {})
    file_content = root_fields.get("fileContent", {}).get("value")
    if not file_content:
        raise ShareResolveError("Resolved share does not contain downloadable file content")

    user_filename = extract_user_filename(share_url)
    filename = user_filename or share_fields.get("cloudkit.title", {}).get("value") or "downloaded_file"
    download_template = file_content.get("downloadURL")
    if not download_template:
        raise ShareResolveError("Download URL template missing from CloudKit response")

    download_url = download_template.replace("${f}", quote(filename))

    return {
        "filename": filename,
        "download_url": download_url,
        "size": file_content.get("size"),
    }


def create_session() -> requests.Session:
    """Return a requests session with randomized headers per thread."""
    sess = requests.Session()
    fingerprint = uuid.uuid4().hex
    user_agent = random.choice(USER_AGENT_TEMPLATES)
    sess.headers.update(
        {
            "User-Agent": f"{user_agent} {fingerprint[:6]}",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "Connection": "keep-alive",
            "X-Request-ID": fingerprint,
        }
    )
    return sess


def format_size(num_bytes: float) -> str:
    """Return a short human readable representation of a byte value."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024.0 or unit == "TB":
            return f"{num_bytes:3.1f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f}TB"


def download_file(
    download_url: str,
    destination: Path,
    total_size: Optional[int],
    session: Optional[requests.Session] = None,
    chunk_size: int = 1 << 20,
    progress_bar: Optional[tqdm] = None,
) -> None:
    """Download a file sequentially."""
    _download_sequential(
        download_url,
        destination,
        total_size,
        session,
        chunk_size,
        progress_bar=progress_bar,
    )


def _download_sequential(
    download_url: str,
    destination: Path,
    total_size: Optional[int],
    session: Optional[requests.Session],
    chunk_size: int,
    progress_bar: Optional[tqdm] = None,
) -> None:
    close_after = session is None
    sess = session or create_session()
    downloaded = 0
    start_time = time.monotonic()
    last_update = 0.0

    with sess.get(download_url, stream=True) as response:
        response.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                output.write(chunk)
                downloaded += len(chunk)
                elapsed = max(time.monotonic() - start_time, 1e-6)

                if progress_bar:
                    progress_bar.update(len(chunk))
                    now = time.monotonic()
                    if now - last_update >= 0.2 or (
                        total_size and downloaded >= total_size
                    ):
                        postfix = {"speed": format_size(downloaded / elapsed) + "/s"}
                        if total_size:
                            percent = downloaded * 100 / total_size
                            postfix["percent"] = f"{percent:5.1f}%"
                            postfix["total"] = format_size(total_size)
                        progress_bar.set_postfix(postfix, refresh=False)
                        last_update = now
                else:
                    speed = format_size(downloaded / elapsed) + "/s"
                    if total_size:
                        percent = downloaded * 100 / total_size
                        status = (
                            f"\rDownloading {destination.name}: "
                            f"{downloaded:,}/{total_size:,} bytes ({percent:.1f}%) "
                            f"@ {speed}"
                        )
                    else:
                        status = (
                            f"\rDownloading {destination.name}: {downloaded:,} bytes "
                            f"@ {speed}"
                        )
                    sys.stdout.write(status)
                    sys.stdout.flush()

    if progress_bar:
        elapsed_total = max(time.monotonic() - start_time, 1e-6)
        postfix = {"speed": format_size(downloaded / elapsed_total) + "/s"}
        if total_size:
            percent = downloaded * 100 / total_size
            postfix["percent"] = f"{percent:5.1f}%"
            postfix["total"] = format_size(total_size)
        progress_bar.set_postfix(postfix, refresh=True)
    else:
        sys.stdout.write("\n")

    if close_after:
        sess.close()


def load_share_links(config_path: Path) -> list[str]:
    """Load share URLs from a JSON config file."""
    with config_path.expanduser().open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if isinstance(data, dict):
        share_urls = data.get("share_urls")
    else:
        share_urls = data

    if not isinstance(share_urls, list):
        raise ValueError("inst.json must contain a list of share URLs or a 'share_urls' array.")

    urls: list[str] = []
    for value in share_urls:
        if not isinstance(value, str):
            raise ValueError("Share URLs in inst.json must all be strings.")
        urls.append(value)
    return urls


def prepare_downloads(share_urls: list[str]) -> list[Dict[str, Optional[str]]]:
    """Resolve metadata for each share URL and return a sorted list."""
    downloads: list[Dict[str, Optional[str]]] = []
    for share_url in share_urls:
        short_guid = extract_short_guid(share_url)
        with create_session() as session:
            resolved = resolve_share(short_guid, session=session)
        metadata = build_download_metadata(resolved, share_url)
        downloads.append(metadata)

    downloads.sort(key=lambda item: item["filename"] or "")
    return downloads


def download_share(
    metadata: Dict[str, Optional[str]],
    downloads_dir: Path,
    position: int,
    total_progress: tqdm,
) -> Path:
    """Download a single share using a tqdm progress bar."""
    filename = metadata["filename"] or "downloaded_file"
    download_url = metadata["download_url"]
    raw_size = metadata["size"]

    total_size: Optional[int]
    if isinstance(raw_size, int):
        total_size = raw_size
    elif isinstance(raw_size, str):
        try:
            total_size = int(raw_size)
        except ValueError:
            total_size = None
    else:
        total_size = None

    destination = downloads_dir / filename

    bar = tqdm(
        total=total_size,
        desc=filename,
        position=position,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        dynamic_ncols=True,
        leave=True,
    )
    success = False
    try:
        if total_progress is not None:
            total_progress.set_postfix(file=filename, refresh=False)
        with create_session() as session:
            download_file(
                download_url,
                destination,
                total_size,
                session=session,
                progress_bar=bar,
            )
        success = True
    finally:
        bar.close()
        if success:
            tqdm.write(f"Completed {filename}")
    return destination


def download_multiple(share_urls: list[str], output_dir: str = ".") -> None:
    """Download multiple iCloud share links concurrently with progress bars."""
    if not share_urls:
        print("No share URLs provided.")
        return

    downloads_dir = Path(output_dir).expanduser().resolve() / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    downloads = prepare_downloads(share_urls)
    if not downloads:
        print("No downloadable items found.")
        return

    tqdm.write(f"Starting concurrent download for {len(downloads)} share(s).")

    lock = threading.Lock()

    def run_download(metadata: Dict[str, Optional[str]], index: int) -> None:
        download_share(metadata, downloads_dir, index, None)

    with ThreadPoolExecutor(max_workers=len(downloads)) as executor:
        future_map = {
            executor.submit(run_download, metadata, index): metadata["filename"]
            for index, metadata in enumerate(downloads)
        }
        for future in as_completed(future_map):
            filename = future_map[future] or "downloaded_file"
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                tqdm.write(f"Failed {filename}: {exc}")


def main(share_url: str, output_dir: str = ".") -> None:
    """
    Resolve an iCloud Drive share link and download the referenced file.

    When run directly you can also override the share link by passing it
    as the first CLI argument, and optionally a second argument for the
    output directory.
    """
    if len(sys.argv) > 1:
        share_url = sys.argv[1]
    if len(sys.argv) > 2:
        output_dir = sys.argv[2]

    download_multiple([share_url], output_dir)


if __name__ == "__main__":
    config_path = Path(__file__).with_name("inst.json")
    try:
        share_links = load_share_links(config_path)
    except FileNotFoundError:
        print(f"Configuration file not found: {config_path}")
        sys.exit(1)
    except ValueError as exc:
        print(f"Invalid configuration in {config_path}: {exc}")
        sys.exit(1)

    download_multiple(share_links)
