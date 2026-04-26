# LINE Sticker Tools

A suite of tools for downloading and organising LINE sticker packs.

## Tools

| Tool | Language | Purpose |
|------|----------|---------|
| [downloader](downloader/) | Python 3 | Downloads sticker packs by ID range from LINE's CDN |
| [renamer](renamer/) | C++ (C++17) | Renames downloaded sticker directories using metadata from `productinfo.meta` |

## Typical Workflow

1. Run the **downloader** to fetch sticker packs into the `unzipped/` directory
2. Run the **renamer** inside `unzipped/` to rename each directory to a human-readable name

Renamed directories follow this format:

```
[packageId] {Author Name} Sticker Title (A) (S) (P)
```

Where the flags indicate optional features:
- `(A)` — animated
- `(S)` — has sound
- `(P)` — popup type

Flags are omitted if the feature is not present. Multiple flags appear as separate parentheses, e.g. `(A) (S)`.

## Prerequisites

See the README in each tool's subdirectory for specific setup instructions.

## Disclaimer

These tools interact with LINE's CDN. Use for personal purposes only and respect LINE's terms of service. The authors take no responsibility for misuse.
