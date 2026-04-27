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

START_ID     = 19000
END_ID       = 19026

DOWNLOAD_DIR = "downloads"
UNZIP_DIR    = "unzipped"
LOG_FILE     = "results.jsonl"   # one JSON object per line — safe to append

MAX_WORKERS  = 12        # tune to your connection; 12 is a reasonable ceiling
TIMEOUT      = 15        # seconds per request
CHUNK_SIZE   = 256_000   # 256 KB chunks — fewer syscalls on large zips

# Maximum number of download+integrity attempts per ID before giving up
MAX_ATTEMPTS = 3

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

_log_lock   = threading.Lock()
_log_handle = None

def _open_log() -> None:
    global _log_handle
    _log_handle = open(LOG_FILE, "a", encoding="utf-8", buffering=1)

def _close_log() -> None:
    if _log_handle:
        _log_handle.flush()
        _log_handle.close()

def _log(entry: dict) -> None:
    """Append a JSON result entry to the shared log handle (thread-safe)."""
    with _log_lock:
        _log_handle.write(json.dumps(entry) + "\n")


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
# Integrity check
# ---------------------------------------------------------------------------

def _integrity_check(extract_path: str) -> str | None:
    """
    Lightweight post-extraction integrity check.
    Returns None on pass, or an error string describing the failure.

    Checks:
      1. Extract directory exists and is not empty
      2. productinfo.meta is present and valid JSON
      3. meta contains required fields: packageId, title, author
      4. At least one PNG file exists in the directory
    """
    # 1. Directory exists and is not empty
    if not os.path.isdir(extract_path):
        return "extract directory missing"
    if not os.listdir(extract_path):
        return "extract directory is empty"

    # 2. productinfo.meta present and parseable
    meta_path = os.path.join(extract_path, "productinfo.meta")
    if not os.path.exists(meta_path):
        return "productinfo.meta missing"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except json.JSONDecodeError as e:
        return f"productinfo.meta invalid JSON: {e}"

    # 3. Required fields present
    for field in ("packageId", "title", "author"):
        if field not in meta:
            return f"productinfo.meta missing field: {field}"

    # 4. At least one PNG exists anywhere in the extracted directory
    found_png = False
    for root, _, files in os.walk(extract_path):
        if any(f.lower().endswith(".png") for f in files):
            found_png = True
            break
    if not found_png:
        return "no PNG files found in extracted pack"

    return None  # all checks passed

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
            os.remove(zip_path)
        except OSError:
            pass
        return "bad zip"

# ---------------------------------------------------------------------------
# Core worker
# ---------------------------------------------------------------------------

def _download(sticker_id: int, zip_path: str) -> tuple[str | None, str | None]:
    """
    Try each URL in PACK_URLS in order.
    Returns (pack_type, error).
    """
    last_err: str | None = None

    for url_template, label in zip(PACK_URLS, PACK_LABELS):
        url = url_template.format(id=sticker_id)
        try:
            with _session().get(url, timeout=TIMEOUT, stream=True) as r:
                if r.status_code == 404:
                    last_err = "not found on any URL"
                    continue
                r.raise_for_status()
                tmp_path = zip_path + ".part"
                try:
                    with open(tmp_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                            if chunk:
                                f.write(chunk)
                    os.replace(tmp_path, zip_path)
                except Exception:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    raise
                return label, None
        except requests.RequestException as e:
            last_err = f"{label} request failed: {e}"
            continue

    return None, last_err or "not found on any URL"


def _purge_extract(extract_path: str) -> None:
    """Remove a failed extraction directory so the next attempt starts clean."""
    import shutil
    try:
        if os.path.isdir(extract_path):
            shutil.rmtree(extract_path)
    except OSError:
        pass


def process(sticker_id: int) -> dict:
    """
    Download, extract, and integrity-check one sticker pack.
    On integrity failure, deletes the bad extraction and retries the full
    download+extract cycle up to MAX_ATTEMPTS times before giving up.
    Returns a result dict: {id, status, pack_type, message, attempts}
    """
    extract_path = os.path.join(UNZIP_DIR, str(sticker_id))
    zip_path     = os.path.join(DOWNLOAD_DIR, f"{sticker_id}.zip")

    # Already fully extracted and integrity-checked from a previous run
    if os.path.isdir(extract_path):
        err = _integrity_check(extract_path)
        if err is None:
            return {"id": sticker_id, "status": "skip", "pack_type": None,
                    "message": "already extracted", "attempts": 0}
        # Existing extraction is corrupt — purge and re-download
        _purge_extract(extract_path)
        try:
            os.remove(zip_path)
        except OSError:
            pass

    pack_type = None

    for attempt in range(1, MAX_ATTEMPTS + 1):

        # Download if zip not on disk
        if not os.path.exists(zip_path):
            pack_type, err = _download(sticker_id, zip_path)
            if err:
                status = "404" if "not found" in err else "fail"
                return {"id": sticker_id, "status": status, "pack_type": None,
                        "message": err, "attempts": attempt}

        # Extract
        err = _unzip(sticker_id, zip_path, extract_path)
        if err:
            # Bad zip — already deleted by _unzip, retry download
            if attempt < MAX_ATTEMPTS:
                continue
            return {"id": sticker_id, "status": "fail", "pack_type": pack_type,
                    "message": f"bad zip after {attempt} attempt(s)", "attempts": attempt}

        # Integrity check
        err = _integrity_check(extract_path)
        if err is None:
            # All good
            return {"id": sticker_id, "status": "ok", "pack_type": pack_type,
                    "message": f"downloaded ({pack_type}) & verified",
                    "attempts": attempt}

        # Integrity failed — purge and retry
        _purge_extract(extract_path)
        try:
            os.remove(zip_path)
        except OSError:
            pass

        if attempt == MAX_ATTEMPTS:
            return {"id": sticker_id, "status": "fail", "pack_type": pack_type,
                    "message": f"integrity check failed after {attempt} attempt(s): {err}",
                    "attempts": attempt}

    # Should not reach here
    return {"id": sticker_id, "status": "fail", "pack_type": None,
            "message": "max attempts exceeded", "attempts": MAX_ATTEMPTS}

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def _cleanup_downloads(counts: dict) -> None:
    """
    Remove the downloads folder once the run is fully complete with no failures.
    Keeps the folder if any failures occurred so the next run can resume.
    """
    import shutil

    if not os.path.isdir(DOWNLOAD_DIR):
        return

    if counts.get("fail", 0) > 0:
        print(f"Keeping {DOWNLOAD_DIR}/ — {counts['fail']} failed pack(s) "
              f"may have partial zips needed for resume.")
        return

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
