/*
 * line_renamer.cpp
 *
 * Scans every subdirectory in the current working directory, reads the
 * productinfo.meta JSON file inside each one, and renames the directory to:
 *   [packageId] {author} title (A) (S) (P)
 *
 * Each feature flag gets its own parentheses:
 *   (A) = hasAnimation
 *   (S) = hasSound
 *   (P) = stickerResourceType contains "POPUP"
 * Flags are omitted entirely if the feature is not present.
 *
 * Build — single architecture (auto-detected):
 *   g++ -std=c++17 -O2 -pthread -o line_renamer line_renamer.cpp
 *
 * Build — universal binary (Intel + Apple Silicon):
 *   g++ -std=c++17 -O2 -pthread -target x86_64-apple-macos10.15 -o line_renamer_x86 line_renamer.cpp
 *   g++ -std=c++17 -O2 -pthread -target arm64-apple-macos11    -o line_renamer_arm64 line_renamer.cpp
 *   lipo -create -output line_renamer line_renamer_x86 line_renamer_arm64
 *
 * Requires: C++17 (std::filesystem), POSIX threads, Xcode Command Line Tools.
 */

#include <filesystem>
#include <fstream>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
#include <algorithm>
#include <atomic>

namespace fs = std::filesystem;

/* -------------------------------------------------------------------------
 * Minimal JSON field extractor
 *
 * No third-party dependencies. We do targeted extraction on the known
 * schema rather than a full parse:
 *   raw_value(json, key)        - raw text of the value for a top-level key
 *   extract_string(json, key)   - unescaped quoted string value
 *   extract_bool(json, key)     - boolean value
 *   extract_nested_en(json, k)  - string at json[k]["en"]
 * ------------------------------------------------------------------------- */

static std::string raw_value(const std::string& json, const std::string& key)
{
    std::string needle = "\"" + key + "\"";
    auto pos = json.find(needle);
    if (pos == std::string::npos) return {};

    pos += needle.size();
    while (pos < json.size() && (json[pos]==' '||json[pos]=='\t'||json[pos]=='\n'||json[pos]=='\r'))
        ++pos;
    if (pos >= json.size() || json[pos] != ':') return {};
    ++pos;
    while (pos < json.size() && (json[pos]==' '||json[pos]=='\t'||json[pos]=='\n'||json[pos]=='\r'))
        ++pos;
    if (pos >= json.size()) return {};

    int depth = 0;
    bool in_str = false;
    std::string result;
    for (; pos < json.size(); ++pos) {
        char c = json[pos];
        if (in_str) {
            if (c == '\\') { result += c; ++pos; if (pos < json.size()) result += json[pos]; continue; }
            if (c == '"') in_str = false;
            result += c;
        } else {
            if (c == '"')  { in_str = true; result += c; continue; }
            if (c == '{'||c == '[') ++depth;
            if (c == '}'||c == ']') { if (depth == 0) break; --depth; }
            if (c == ',' && depth == 0) break;
            result += c;
        }
    }
    return result;
}

static std::string extract_string(const std::string& json, const std::string& key)
{
    std::string rv = raw_value(json, key);
    if (rv.empty() || rv.front() != '"') return {};
    std::string s = rv.substr(1, rv.size() >= 2 ? rv.size() - 2 : 0);

    std::string out;
    out.reserve(s.size());
    for (size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '\\' && i + 1 < s.size()) {
            char n = s[i+1];
            if      (n == '"')  { out += '"';  ++i; }
            else if (n == '\\') { out += '\\'; ++i; }
            else if (n == 'n')  { out += '\n'; ++i; }
            else if (n == 't')  { out += '\t'; ++i; }
            else if (n == 'u' && i + 5 < s.size()) {
                unsigned cp = std::stoul(s.substr(i+2, 4), nullptr, 16);
                if      (cp == 0x26) out += '&';
                else if (cp == 0x27) out += '\'';
                else                 out += '?';
                i += 5;
            } else { out += s[i]; }
        } else {
            out += s[i];
        }
    }
    return out;
}

static bool extract_bool(const std::string& json, const std::string& key)
{
    return raw_value(json, key) == "true";
}

static std::string extract_nested_en(const std::string& json, const std::string& outer_key)
{
    std::string block = raw_value(json, outer_key);
    if (block.empty() || block.front() != '{') return {};
    return extract_string(block, "en");
}

/* -------------------------------------------------------------------------
 * String sanitisation
 * ------------------------------------------------------------------------- */

static void replace_all(std::string& s, const std::string& from, const std::string& to)
{
    if (from.empty()) return;
    size_t pos = 0;
    while ((pos = s.find(from, pos)) != std::string::npos) {
        s.replace(pos, from.size(), to);
        pos += to.size();
    }
}

static std::string sanitize(std::string s)
{
    replace_all(s, ":", "-");
    replace_all(s, "/", "_");
    size_t start = s.find_first_not_of(" \t\n\r");
    size_t end   = s.find_last_not_of(" \t\n\r");
    if (start == std::string::npos) return {};
    return s.substr(start, end - start + 1);
}

/* -------------------------------------------------------------------------
 * Build new directory name from JSON content
 * Returns empty string and sets err_msg on failure.
 * ------------------------------------------------------------------------- */

static std::string build_dirname(const std::string& json, std::string& err_msg)
{
    std::string pkg_id = raw_value(json, "packageId");
    if (pkg_id.empty()) { err_msg = "missing 'packageId'"; return {}; }
    pkg_id.erase(0, pkg_id.find_first_not_of(" \t"));
    pkg_id.erase(pkg_id.find_last_not_of(" \t") + 1);

    std::string author = sanitize(extract_nested_en(json, "author"));
    if (author.empty()) { err_msg = "missing or malformed 'author.en'"; return {}; }

    std::string title = sanitize(extract_nested_en(json, "title"));
    if (title.empty()) { err_msg = "missing or malformed 'title.en'"; return {}; }

    std::string flags;
    if (extract_bool(json, "hasAnimation")) flags += 'A';
    if (extract_bool(json, "hasSound"))     flags += 'S';
    std::string res_type = extract_string(json, "stickerResourceType");
    if (res_type.find("POPUP") != std::string::npos) flags += 'P';

    // Each flag gets its own parentheses: (A)(S)(P)
    std::string suffix;
    for (char f : flags)
        suffix += std::string(" (") + f + ")";

    return "[" + pkg_id + "] {" + author + "} " + title + suffix;
}

/* -------------------------------------------------------------------------
 * Per-directory work unit  (called from worker threads)
 * ------------------------------------------------------------------------- */

struct Result {
    std::string original;
    bool        success;
    std::string message;
};

static std::mutex g_rename_mutex;

static Result process_directory(const fs::path& dir)
{
    Result res;
    res.original = dir.filename().string();
    res.success  = false;

    fs::path meta_path = dir / "productinfo.meta";
    if (!fs::is_regular_file(meta_path)) {
        res.message = "  SKIP  '" + res.original + "': no productinfo.meta found";
        return res;
    }

    std::ifstream ifs(meta_path);
    if (!ifs) {
        res.message = "  ERROR '" + res.original + "': could not open meta file";
        return res;
    }
    std::ostringstream buf;
    buf << ifs.rdbuf();
    std::string json = buf.str();

    std::string err_msg;
    std::string new_name = build_dirname(json, err_msg);
    if (new_name.empty()) {
        res.message = "  ERROR '" + res.original + "': " + err_msg;
        return res;
    }

    // Lock for the exists-check + rename to prevent TOCTOU race between threads
    {
        std::lock_guard<std::mutex> lock(g_rename_mutex);

        fs::path target = dir.parent_path() / new_name;
        if (fs::exists(target)) {
            res.message = "  SKIP  '" + res.original + "': target already exists — '" + new_name + "'";
            return res;
        }

        std::error_code ec;
        fs::rename(dir, target, ec);
        if (ec) {
            res.message = "  ERROR '" + res.original + "': rename failed — " + ec.message();
            return res;
        }
    }

    res.success = true;
    res.message = "  OK    '" + res.original + "' -> '" + new_name + "'";
    return res;
}

/* -------------------------------------------------------------------------
 * Thread pool using atomic work index (lock-free task dispatch)
 * ------------------------------------------------------------------------- */

static std::vector<Result> run_threaded(const std::vector<fs::path>& dirs)
{
    const unsigned int n_threads = std::min(
        static_cast<unsigned int>(32),
        std::max(std::thread::hardware_concurrency(), 1u)
    );

    std::vector<Result> results(dirs.size());
    std::atomic<size_t> next_idx{0};
    std::vector<std::thread> threads;
    threads.reserve(n_threads);

    auto worker = [&]() {
        for (;;) {
            size_t idx = next_idx.fetch_add(1, std::memory_order_relaxed);
            if (idx >= dirs.size()) break;
            results[idx] = process_directory(dirs[idx]);
        }
    };

    for (unsigned int i = 0; i < n_threads; ++i)
        threads.emplace_back(worker);
    for (auto& t : threads)
        t.join();

    return results;
}

/* -------------------------------------------------------------------------
 * Entry point
 * ------------------------------------------------------------------------- */

int main()
{
    fs::path cwd = fs::current_path();

    std::vector<fs::path> dirs;
    for (const auto& entry : fs::directory_iterator(cwd))
        if (entry.is_directory())
            dirs.push_back(entry.path());

    if (dirs.empty()) {
        std::cout << "No subdirectories found.\n";
        return 0;
    }

    std::sort(dirs.begin(), dirs.end());

    std::vector<Result> results = run_threaded(dirs);

    std::sort(results.begin(), results.end(),
        [](const Result& a, const Result& b){ return a.original < b.original; });

    int ok = 0, skipped = 0, errors = 0;
    for (const auto& res : results) {
        std::cout << res.message << '\n';
        if (res.success)
            ++ok;
        else if (res.message.find("SKIP") != std::string::npos)
            ++skipped;
        else
            ++errors;
    }

    std::cout << "\nDone -- " << ok << " renamed, "
              << skipped << " skipped, "
              << errors  << " error(s).\n";
    return 0;
}
