from __future__ import annotations

import json
import random
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import quote, urlparse

import requests

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


class ProgressReporter:
    """Render multi-line progress updates for concurrent downloads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._order: list[str] = []
        self._statuses: Dict[str, str] = {}
        self._rendered = False
        self._rows = 0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def register(self, label: str, initial: str = "0 B/s") -> None:
        with self._lock:
            if label not in self._statuses:
                self._order.append(label)
            self._statuses[label] = initial

    def update(self, label: str, status: str) -> None:
        with self._lock:
            if label not in self._statuses:
                self._order.append(label)
            self._statuses[label] = status

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="progress-reporter", daemon=True
            )
            self._thread.start()

    def finalize(self) -> None:
        self._stop_event.set()
        thread: Optional[threading.Thread]
        with self._lock:
            thread = self._thread
        if thread:
            thread.join()
        with self._lock:
            self._thread = None
            self._rendered = False
            self._rows = 0

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._render(stay=True)
            if self._stop_event.wait(1.0):
                break
        self._render(stay=False)

    def _render(self, *, stay: bool) -> None:
        with self._lock:
            order_snapshot = list(self._order)
            status_snapshot = {label: self._statuses[label] for label in order_snapshot}
            rows = len(order_snapshot)
            rendered_before = self._rendered
            prev_rows = self._rows
            self._rendered = self._rendered or bool(order_snapshot)
            if rows > self._rows:
                self._rows = rows
        if not order_snapshot:
            return
        if not rendered_before:
            sys.stdout.write("\n" * rows)
            sys.stdout.write(f"\033[{rows}A")
            sys.stdout.write("\033[s")
        else:
            sys.stdout.write("\033[u")
            if rows > prev_rows:
                sys.stdout.write(f"\033[{prev_rows}B")
                sys.stdout.write("\n" * (rows - prev_rows))
                sys.stdout.write(f"\033[{rows}A")
            sys.stdout.write("\033[s")
        for label in order_snapshot:
            status = status_snapshot.get(label, "")
            display = label or "-"
            sys.stdout.write("\r\033[K")
            sys.stdout.write(f"{display} {status}")
            if label != order_snapshot[-1]:
                sys.stdout.write("\033[B\r")
        if stay:
            sys.stdout.write("\033[B\r")
        else:
            sys.stdout.write("\033[B\r")
            sys.stdout.write("\033[s")
        sys.stdout.flush()

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
    progress_callback: Optional[Callable[[int, Optional[int], str], None]] = None,
) -> None:
    """Download a file sequentially."""
    if total_size is None:
        print("Unknown file size, using single stream download...")
    else:
        print(f"Total size: {total_size:,} bytes, chunk size: {chunk_size:,} bytes")
    _download_sequential(
        download_url,
        destination,
        total_size,
        session,
        chunk_size,
        progress_callback=progress_callback,
    )


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


def download_share(
    share_url: str,
    output_dir: str = ".",
    reporter: Optional[ProgressReporter] = None,
) -> Path:
    """Download a single iCloud share link and return the destination path."""
    label = share_url
    short_guid = extract_short_guid(share_url)
    with create_session() as session:
        try:
            resolved = resolve_share(short_guid, session=session)
            metadata = build_download_metadata(resolved, share_url)
            filename = metadata["filename"]
            label = filename
            download_url = metadata["download_url"]
            size = metadata["size"]

            destination = Path(output_dir).expanduser().resolve() / "downloads" / filename
            print(f"Downloading '{filename}' to {destination}")
            if size:
                print(f"Reported size: {size:,} bytes")
            if reporter:
                reporter.register(label)

                def on_progress(downloaded: int, total: Optional[int], speed: str) -> None:
                    if total:
                        percent = downloaded * 100 / total
                        total_fmt = format_size(total)
                        status = f"{speed} {percent:5.1f}% {total_fmt}"
                    else:
                        status = f"{speed}"
                    reporter.update(label, status)

                download_file(
                    download_url,
                    destination,
                    size,
                    session=session,
                    progress_callback=on_progress,
                )
            else:
                download_file(download_url, destination, size, session=session)
            print(f"Download complete for '{filename}'.")
            return destination
        except Exception as exc:  # noqa: BLE001
            if reporter:
                reporter.register(label)
                reporter.update(label, "Failed")
            raise


def download_multiple(share_urls: list[str], output_dir: str = ".") -> None:
    """Download multiple iCloud share links concurrently."""
    if not share_urls:
        print("No share URLs provided.")
        return

    print(f"Starting concurrent download for {len(share_urls)} share(s).")
    reporter = ProgressReporter()
    reporter.start()
    try:
        with ThreadPoolExecutor(max_workers=len(share_urls)) as executor:
            futures = {
                executor.submit(download_share, url, output_dir, reporter): url
                for url in share_urls
            }
            for future in as_completed(futures):
                url = futures[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"Failed to download '{url}': {exc}")
    finally:
        reporter.finalize()


def _download_sequential(
    download_url: str,
    destination: Path,
    total_size: Optional[int],
    session: Optional[requests.Session],
    chunk_size: int,
    progress_callback: Optional[Callable[[int, Optional[int], str], None]] = None,
) -> None:
    close_after = session is None
    sess = session or create_session()
    downloaded = 0
    start_time = time.monotonic()
    last_update = 0.0
    reported_position = -1

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
                speed = format_size(downloaded / elapsed) + "/s"
                now = time.monotonic()
                if progress_callback and (now - last_update >= 0.2 or downloaded == total_size):
                    progress_callback(downloaded, total_size, speed)
                    last_update = now
                    reported_position = downloaded
                elif not progress_callback:
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
    if progress_callback and downloaded != reported_position:
        elapsed_total = max(time.monotonic() - start_time, 1e-6)
        final_speed = format_size(downloaded / elapsed_total) + "/s"
        progress_callback(downloaded, total_size, final_speed)
    else:
        sys.stdout.write("\n")
    if close_after:
        sess.close()


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
    download_share(share_url, output_dir)


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
