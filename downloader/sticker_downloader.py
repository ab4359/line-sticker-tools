import os
import json
import threading
import zipfile
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# URLs are tried in order for each ID — first hit wins, 404 falls through to the next
PACK_URLS = [
    "http://dl.stickershop.line.naver.jp/products/0/0/1/{id}/iphone/stickerpack@2x.zip",  # animated (checked first)
    "http://dl.stickershop.line.naver.jp/products/0/0/1/{id}/iphone/stickers@2x.zip",     # static (fallback)
]

# Labels must stay in the same order as PACK_URLS
PACK_LABELS = ["animated", "static"]

START_ID     = 100
END_ID       = 120

DOWNLOAD_DIR = "downloads"
UNZIP_DIR    = "unzipped"
LOG_FILE     = "results.jsonl"   # one JSON object per line — safe to append

MAX_WORKERS  = 12        # tune to your connection; 12 is a reasonable ceiling
TIMEOUT      = 15        # seconds per request
CHUNK_SIZE   = 256_000   # 256 KB chunks — fewer syscalls on large zips

# urllib3 retry on transient network errors (NOT on 404 — handled manually)
_RETRY = Retry(
    total=3,
    backoff_factor=1.5,        # waits 0 s, 1.5 s, 3 s between attempts
    status_forcelist={500, 502, 503, 504},
    allowed_methods={"GET", "HEAD"},
    raise_on_status=False,
)

# ---------------------------------------------------------------------------
# Session factory — one persistent session per thread via threading.local
# ---------------------------------------------------------------------------
_local = threading.local()

def _session() -> requests.Session:
    """Return a thread-local Session with a mounted retry adapter."""
    if not hasattr(_local, "session"):
        s = requests.Session()
        adapter = HTTPAdapter(
            max_retries=_RETRY,
            pool_connections=MAX_WORKERS,
            pool_maxsize=MAX_WORKERS,
        )
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _local.session = s
    return _local.session

# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------

# FIX: the original _log() opened and closed the file on every single call —
# once per downloaded pack. At scale that's thousands of open/close syscalls.
# A single shared file handle is faster; the write lock makes it thread-safe.
_log_lock = threading.Lock()
_log_handle: "IO | None" = None

def _open_log() -> None:
    global _log_handle
    _log_handle = open(LOG_FILE, "a", encoding="utf-8", buffering=1)  # line-buffered

def _close_log() -> None:
    if _log_handle:
        _log_handle.flush()
        _log_handle.close()

def _log(entry: dict) -> None:
    """Append a JSON result entry to the shared log handle (thread-safe)."""
    line = json.dumps(entry) + "\n"
    with _log_lock:
        _log_handle.write(line)


def _load_completed() -> set[int]:
    """Read the log file and return IDs that finished successfully or were 404."""
    done = set()
    if not os.path.exists(LOG_FILE):
        return done
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("status") in ("ok", "404", "skip"):
                    done.add(entry["id"])
            except json.JSONDecodeError:
                pass
    return done

# ---------------------------------------------------------------------------
# Zip helper
# ---------------------------------------------------------------------------

def _unzip(sticker_id: int, zip_path: str, extract_path: str) -> str | None:
    """Extract zip; returns an error string on failure, None on success."""
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(extract_path)
        return None
    except zipfile.BadZipFile:
        try:
            os.remove(zip_path)   # corrupt — delete so next run re-downloads
        except OSError:
            pass
        return "bad zip"

# ---------------------------------------------------------------------------
# Core worker
# ---------------------------------------------------------------------------

def _download(sticker_id: int, zip_path: str) -> tuple[str | None, str | None]:
    """
    Try each URL in PACK_URLS in order.
    Returns (pack_type, error): pack_type is "animated"/"static" on success.

    BUG FIX: the original code returned immediately on ANY RequestException,
    meaning a timeout or connection error on the animated URL would skip the
    static URL entirely rather than falling through to try it. Now network
    errors on one URL fall through to the next just like a 404 does, and only
    a hard failure on the *last* URL returns an error.
    """
    last_err: str | None = None

    for url_template, label in zip(PACK_URLS, PACK_LABELS):
        url = url_template.format(id=sticker_id)
        try:
            with _session().get(url, timeout=TIMEOUT, stream=True) as r:
                if r.status_code == 404:
                    last_err = "not found on any URL"
                    continue          # try the next URL
                r.raise_for_status()
                # Write to a temp path then rename — avoids leaving a partial
                # zip on disk if the process is killed mid-download, which would
                # cause the next run to skip the download and try to unzip garbage.
                tmp_path = zip_path + ".part"
                try:
                    with open(tmp_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                            if chunk:
                                f.write(chunk)
                    os.replace(tmp_path, zip_path)   # atomic on POSIX
                except Exception:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    raise
                return label, None    # success
        except requests.RequestException as e:
            # FIX: fall through to next URL instead of returning immediately
            last_err = f"{label} request failed: {e}"
            continue

    return None, last_err or "not found on any URL"


def process(sticker_id: int) -> dict:
    """
    Download and unzip one sticker pack, trying PACK_URLS in priority order.
    Returns a result dict: {id, status, pack_type, message}
    """
    extract_path = os.path.join(UNZIP_DIR, str(sticker_id))
    zip_path     = os.path.join(DOWNLOAD_DIR, f"{sticker_id}.zip")

    # Already fully extracted from a previous run
    if os.path.isdir(extract_path):
        return {"id": sticker_id, "status": "skip", "pack_type": None,
                "message": "already extracted"}

    # Download — skip if a complete zip is already on disk
    pack_type = None
    if not os.path.exists(zip_path):
        pack_type, err = _download(sticker_id, zip_path)
        if err:
            status = "404" if "not found" in err else "fail"
            return {"id": sticker_id, "status": status, "pack_type": None,
                    "message": err}

    # Unzip
    err = _unzip(sticker_id, zip_path, extract_path)
    if err:
        return {"id": sticker_id, "status": "fail", "pack_type": pack_type,
                "message": err}

    return {"id": sticker_id, "status": "ok", "pack_type": pack_type,
            "message": f"downloaded ({pack_type}) & extracted"}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(UNZIP_DIR, exist_ok=True)

    all_ids   = range(START_ID, END_ID + 1)
    completed = _load_completed()
    pending   = [i for i in all_ids if i not in completed]

    total = END_ID - START_ID + 1
    print(f"Range: {START_ID} → {END_ID}  ({total:,} total, "
          f"{len(completed):,} already done, {len(pending):,} to process)")

    if not pending:
        print("Nothing to do.")
        return

    counts = {"ok": 0, "404": 0, "skip": 0, "fail": 0}

    _open_log()
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(process, i): i for i in pending}

            with tqdm(total=len(pending), unit="pack", dynamic_ncols=True) as bar:
                for future in as_completed(futures):
                    result = future.result()
                    _log(result)

                    status = result["status"]
                    counts[status] = counts.get(status, 0) + 1

                    bar.set_postfix(
                        ok=counts["ok"],
                        f404=counts["404"],
                        fail=counts["fail"],
                        refresh=False,
                    )
                    bar.update(1)
    finally:
        _close_log()



def _cleanup_downloads(counts: dict) -> None:
    """
    Remove the downloads folder and all zips inside it, but only when it is
    safe to do so:
      - No failures (counts["fail"] == 0) — failed packs may have a partial
        zip on disk that the next run needs to overwrite cleanly.
      - No outstanding .part files — indicates a download was interrupted.
      - The downloads folder actually exists.

    If any condition isn't met, we leave the folder alone and tell the user why.
    """
    import shutil

    if not os.path.isdir(DOWNLOAD_DIR):
        return  # nothing to clean up

    if counts.get("fail", 0) > 0:
        print(f"Keeping {DOWNLOAD_DIR}/ — {counts['fail']} failed pack(s) "
              f"may have partial zips needed for resume.")
        return

    # Check for any leftover .part files from interrupted downloads
    part_files = [f for f in os.listdir(DOWNLOAD_DIR) if f.endswith(".part")]
    if part_files:
        print(f"Keeping {DOWNLOAD_DIR}/ — {len(part_files)} incomplete "
              f"download(s) still present (.part files).")
        return

    try:
        shutil.rmtree(DOWNLOAD_DIR)
        print(f"Cleaned up: {DOWNLOAD_DIR}/ removed "
              f"({counts.get('ok', 0):,} zip(s) deleted).")
    except OSError as e:
        print(f"Could not remove {DOWNLOAD_DIR}/: {e}")


def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(UNZIP_DIR, exist_ok=True)

    all_ids   = range(START_ID, END_ID + 1)
    completed = _load_completed()
    pending   = [i for i in all_ids if i not in completed]

    total = END_ID - START_ID + 1
    print(f"Range: {START_ID} → {END_ID}  ({total:,} total, "
          f"{len(completed):,} already done, {len(pending):,} to process)")

    if not pending:
        print("Nothing to do.")
        _cleanup_downloads({"fail": 0, "ok": 0})
        return

    counts = {"ok": 0, "404": 0, "skip": 0, "fail": 0}

    _open_log()
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(process, i): i for i in pending}

            with tqdm(total=len(pending), unit="pack", dynamic_ncols=True) as bar:
                for future in as_completed(futures):
                    result = future.result()
                    _log(result)

                    status = result["status"]
                    counts[status] = counts.get(status, 0) + 1

                    bar.set_postfix(
                        ok=counts["ok"],
                        f404=counts["404"],
                        fail=counts["fail"],
                        refresh=False,
                    )
                    bar.update(1)
    finally:
        _close_log()

    print(f"\nDone — {counts['ok']:,} downloaded, {counts['404']:,} not found, "
          f"{counts['skip']:,} skipped, {counts['fail']:,} failed.")
    print(f"Results logged to: {LOG_FILE}")
    _cleanup_downloads(counts)


if __name__ == "__main__":
    main()
