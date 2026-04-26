# Sticker Downloader

Downloads LINE sticker packs by iterating through a range of pack IDs. For each ID it tries the animated pack URL first, falling back to the static pack URL if not found. Successfully downloaded packs are unzipped automatically. Progress is logged to `results.jsonl` so interrupted runs resume cleanly.

## Prerequisites

Python 3.10 or later, plus two dependencies:

```bash
pip3 install requests tqdm
```

## Configuration

Open `sticker_downloader_r2.py` and adjust the constants at the top of the file:

| Constant | Default | Description |
|----------|---------|-------------|
| `START_ID` | `19000` | First pack ID to attempt |
| `END_ID` | `19026` | Last pack ID to attempt (inclusive) |
| `MAX_WORKERS` | `12` | Parallel download threads — tune to your connection |
| `DOWNLOAD_DIR` | `downloads` | Temporary folder for zip files |
| `UNZIP_DIR` | `unzipped` | Destination folder for extracted packs |
| `LOG_FILE` | `results.jsonl` | Resume log — records completed IDs |
| `TIMEOUT` | `15` | Per-request timeout in seconds |
| `CHUNK_SIZE` | `256000` | Download chunk size in bytes |

> **Note:** `START_ID` and `END_ID` are set to a small test range by default. Set them to your intended range before running.

## Usage

```bash
cd downloader
python3 sticker_downloader_r2.py
```

## Output

```
Range: 19000 → 19026  (27 total, 0 already done, 27 to process)
100%|████████████| 27/27 [00:14<00:00, ok=12, f404=14, fail=1]

Done — 12 downloaded, 14 not found, 0 skipped, 1 failed.
Results logged to: results.jsonl
Cleaned up: downloads/ removed (12 zip(s) deleted).
```

The `downloads/` folder is automatically removed once all packs are successfully downloaded and extracted. If any failures occurred it is kept so the next run can resume from existing zips.

## Resume Behaviour

The script reads `results.jsonl` on startup and skips any ID already marked `ok`, `404`, or `skip`. To force a full re-download, delete `results.jsonl`.

## URL Priority

For each ID the following URLs are tried in order:

1. `stickerpack@2x.zip` — animated pack
2. `stickers@2x.zip` — static pack

A 404 on the first URL falls through to the second. Only if both return 404 is the ID recorded as not found.
