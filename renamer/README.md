# Directory Renamer

Scans every subdirectory in the current working directory, reads the `productinfo.meta` JSON file inside each one, and renames the directory to a human-readable name.

## Output Format

```
[packageId] {Author Name} Sticker Title (A) (S) (P)
```

Feature flags — each in its own parentheses, omitted if not present:

| Flag | Meaning |
|------|---------|
| `(A)` | `hasAnimation: true` |
| `(S)` | `hasSound: true` |
| `(P)` | `stickerResourceType` contains `POPUP` |

### Examples

```
[25204] {BitToon} BitToon V2 (A) (S)
[11432] {Line Friends} Brown & Friends
[98712] {Some Creator} Popup Pack (A) (P)
```

## Prerequisites

- macOS or Linux
- Xcode Command Line Tools (macOS) or GCC (Linux)

Install Xcode Command Line Tools on macOS if not already present:

```bash
xcode-select --install
```

## Building

### Single architecture (auto-detected)

```bash
g++ -std=c++17 -O2 -pthread -o line_renamer line_renamer.cpp
```

### Universal binary — Intel + Apple Silicon (macOS only)

```bash
g++ -std=c++17 -O2 -pthread -target x86_64-apple-macos10.15 -o line_renamer_x86 line_renamer.cpp
g++ -std=c++17 -O2 -pthread -target arm64-apple-macos11    -o line_renamer_arm64 line_renamer.cpp
lipo -create -output line_renamer line_renamer_x86 line_renamer_arm64
```

The universal binary runs natively on both Intel and Apple Silicon Macs with no performance penalty on either architecture.

## Usage

Run from inside the directory containing your sticker subdirectories:

```bash
cd /path/to/unzipped
./line_renamer
```

## Output

```
  OK    '25204' → '[25204] {BitToon} BitToon V2 (A) (S)'
  SKIP  '99999': target already exists — '[999] {Jane_Doe} Cool Stickers'
  ERROR 'baddir': missing or malformed 'author.en'
  SKIP  'emptydir': no productinfo.meta found

Done — 1 renamed, 2 skipped, 1 error(s).
```

## Error Handling

| Status | Meaning |
|--------|---------|
| `OK` | Directory successfully renamed |
| `SKIP` | No `productinfo.meta` found, or target name already exists |
| `ERROR` | Meta file unreadable, invalid JSON, missing required fields, or rename failed |

Errors are non-fatal — the tool continues processing remaining directories and reports a summary count at the end.

## Notes

- The compiled binary is excluded from this repository via `.gitignore` — you must build it locally
- Processing is multithreaded; the rename operation itself is protected by a mutex to prevent race conditions
- On macOS, ensure Terminal has Full Disk Access if renaming directories outside your home folder (`System Settings → Privacy & Security → Full Disk Access`)
