"""=====================================
Reads target URLs from targets.txt, downloads recursively with auth,
preserves folder structure, skips already-downloaded files, and appends
per-hub metadata (duration, size) to download_metadata.tsv after each hub.

Requirements:
    pip install playwright requests beautifulsoup4 mutagen
    playwright install chromium

Usage:
    python3 childes_scraper.py --username you@email.com --password yourpassword
    python3 childes_scraper.py --username ... --password ... --show-browser
    python3 childes_scraper.py --username ... --password ... --targets-file my_urls.txt
"""

import os
import re
import time
import csv
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    raise SystemExit("Run:  pip install requests beautifulsoup4")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    raise SystemExit("Run:  pip install playwright && playwright install chromium")

try:
    from mutagen.mp3 import MP3
    from mutagen.wave import WAVE
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False
    print("[WARN] mutagen not installed — audio duration won't be tracked.")
    print("       Run:  pip install mutagen\n")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MEDIA_HOST    = "media.talkbank.org"
DEFAULT_DIR   = os.path.abspath("./childes_audio_staging")
METADATA_FILE = os.path.abspath("./download_metadata.tsv")
TARGETS_FILE  = os.path.abspath("./targets.txt")
REQUEST_DELAY = 0.3
TARGET_EXTENSIONS = {".wav", ".mp3", ".cha"}


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------

def normalise(url: str) -> str:
    """Strip :443, collapse double-slashes, remove trailing slash and query."""
    p = urlparse(url)
    netloc = re.sub(r":443$", "", p.netloc)
    path   = re.sub(r"/+", "/", p.path).rstrip("/")
    return urlunparse((p.scheme, netloc, path, "", "", ""))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def browser_login(username: str, password: str, headless: bool = True) -> dict:
    print("[AUTH] Launching browser...")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx  = browser.new_context()
        page = ctx.new_page()

        page.goto(f"https://{MEDIA_HOST}/", wait_until="networkidle", timeout=30_000)

        try:
            page.click("text=Login", timeout=8_000)
            page.wait_for_timeout(1_000)
        except PWTimeout:
            pass

        page.wait_for_selector("input", timeout=10_000)
        inputs = page.query_selector_all("input")

        for inp in inputs:
            typ = (inp.get_attribute("type") or "text").lower()
            if typ in ("hidden", "password", "submit", "button", "checkbox", "radio"):
                continue
            inp.scroll_into_view_if_needed()
            inp.click()
            inp.fill("")
            inp.type(username, delay=50)
            print(f"[AUTH] Filled email.")
            break

        for inp in inputs:
            if (inp.get_attribute("type") or "").lower() == "password":
                inp.scroll_into_view_if_needed()
                inp.click()
                inp.fill("")
                inp.type(password, delay=50)
                print("[AUTH] Filled password.")
                break

        try:
            btns = page.query_selector_all("button, input[type='submit']")
            for btn in btns:
                txt = btn.inner_text() if btn.get_attribute("type") != "submit" \
                      else (btn.get_attribute("value") or "")
                if "login" in txt.lower():
                    btn.scroll_into_view_if_needed()
                    btn.click()
                    break
            else:
                page.keyboard.press("Enter")
        except Exception:
            page.keyboard.press("Enter")

        try:
            page.wait_for_function(
                "() => !document.querySelector('input[type=\"password\"]')",
                timeout=15_000,
            )
            print("[AUTH] Login success.")
        except PWTimeout:
            body = page.inner_text("body").lower()
            if any(k in body for k in ("invalid", "incorrect", "wrong", "failed")):
                browser.close()
                raise SystemExit("[AUTH] Wrong credentials.")

        cookies = ctx.cookies()
        browser.close()

    if not cookies:
        raise SystemExit("[AUTH] No cookies returned.")
    cookie_dict = {c["name"]: c["value"] for c in cookies}
    print(f"[AUTH] Got cookie(s): {list(cookie_dict.keys())}\n")
    return cookie_dict


# ---------------------------------------------------------------------------
# Audio duration helper
# ---------------------------------------------------------------------------

def get_audio_duration_seconds(filepath: str) -> float:
    """Return duration in seconds, or 0.0 if unreadable."""
    if not HAS_MUTAGEN:
        return 0.0
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".mp3":
            return MP3(filepath).info.length
        elif ext == ".wav":
            return WAVE(filepath).info.length
    except Exception:
        pass
    return 0.0


def fmt_duration(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    return str(timedelta(seconds=int(seconds)))


def fmt_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


# ---------------------------------------------------------------------------
# Folder stats (walk a local directory tree)
# ---------------------------------------------------------------------------

def folder_stats(local_dir: str) -> dict:
    """
    Walk `local_dir` and return:
      total_files, total_size_bytes, total_duration_seconds
    broken down by extension.
    """
    stats = {
        "total_files": 0,
        "total_size_bytes": 0,
        "total_duration_seconds": 0.0,
        "by_ext": {}
    }
    for root, _, files in os.walk(local_dir):
        for f in files:
            fp  = os.path.join(root, f)
            ext = os.path.splitext(f)[1].lower()
            sz  = os.path.getsize(fp)
            dur = get_audio_duration_seconds(fp) if ext in (".mp3", ".wav") else 0.0

            stats["total_files"]            += 1
            stats["total_size_bytes"]        += sz
            stats["total_duration_seconds"]  += dur
            es = stats["by_ext"].setdefault(ext, {"files": 0, "size_bytes": 0, "duration_seconds": 0.0})
            es["files"]            += 1
            es["size_bytes"]       += sz
            es["duration_seconds"] += dur
    return stats


# ---------------------------------------------------------------------------
# Append metadata row to TSV
# ---------------------------------------------------------------------------

METADATA_HEADERS = [
    "timestamp", "hub_url", "local_dir",
    "new_files_downloaded", "skipped_files",
    "total_files_in_folder", "total_size", "total_audio_duration",
    "wav_files", "wav_size", "wav_duration",
    "mp3_files", "mp3_size", "mp3_duration",
    "cha_files", "cha_size",
    "download_elapsed",
]

def append_metadata(row: dict, metadata_file: str) -> None:
    write_header = not os.path.exists(metadata_file)
    with open(metadata_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=METADATA_HEADERS, delimiter="\t",
                                extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    print(f"[META] Appended summary to {metadata_file}")


# ---------------------------------------------------------------------------
# Crawl
# ---------------------------------------------------------------------------

def url_to_local_path(norm_url: str, base_dir: str) -> str:
    rel = urlparse(norm_url).path.lstrip("/")
    return os.path.join(base_dir, rel)


def crawl(session: requests.Session, url: str, base_dir: str,
          visited: set, depth: int = 0) -> tuple[int, int]:
    """Returns (downloaded, skipped)."""
    url = normalise(url)
    if url in visited:
        return 0, 0
    visited.add(url)

    pad = "  " * depth
    print(f"{pad}[DIR] {url}")

    try:
        r = session.get(url + "/", timeout=30, allow_redirects=True)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"{pad}[WARN] {e}")
        return 0, 0

    if urlparse(r.url).path.strip("/") == "login":
        print(f"{pad}[WARN] Redirected to login — session expired.")
        return 0, 0

    soup = BeautifulSoup(r.text, "html.parser")
    downloaded, skipped = 0, 0

    for a in soup.find_all("a", href=True):
        raw  = a["href"].strip()
        if raw in ("..", "../", "./", "/") or raw.startswith(("#", "mailto:", "javascript:")):
            continue

        full  = normalise(urljoin(r.url, raw.split("?")[0]))
        if urlparse(full).hostname != MEDIA_HOST:
            continue

        fname = os.path.basename(urlparse(full).path)
        ext   = os.path.splitext(fname)[1].lower()

        if ext in TARGET_EXTENSIONS:
            local = url_to_local_path(full, base_dir)
            os.makedirs(os.path.dirname(local), exist_ok=True)

            if os.path.exists(local):
                print(f"{pad}  [SKIP] {fname}")
                skipped += 1
                continue

            dl_url = full + "?f=save"
            print(f"{pad}  [DL]   {fname}")
            try:
                fr = session.get(dl_url, timeout=120, stream=True)
                fr.raise_for_status()
                if "text/html" in fr.headers.get("content-type", ""):
                    print(f"{pad}  [WARN] Got HTML for {fname} — auth issue?")
                    continue
                with open(local, "wb") as fh:
                    for chunk in fr.iter_content(65536):
                        fh.write(chunk)
                kb = os.path.getsize(local) / 1024
                print(f"{pad}  [OK]   {fname}  ({kb:.0f} KB)")
                downloaded += 1
                time.sleep(REQUEST_DELAY)
            except requests.RequestException as e:
                print(f"{pad}  [ERR]  {fname}: {e}")

        elif not ext and full != url and full not in visited:
            if urlparse(full).path.startswith(urlparse(url).path):
                time.sleep(REQUEST_DELAY)
                d, s = crawl(session, full, base_dir, visited, depth + 1)
                downloaded += d
                skipped    += s

    return downloaded, skipped


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def dedupe(dl_dir: str) -> None:
    print("\n[DEDUPE] Removing MP3s where WAV exists...")
    removed = 0
    for root, _, files in os.walk(dl_dir):
        bases: dict = {}
        for f in files:
            b, e = os.path.splitext(f)
            if e.lower() in (".wav", ".mp3"):
                bases.setdefault(b, {})[e.lower()] = os.path.join(root, f)
        for b, exts in bases.items():
            if ".wav" in exts and ".mp3" in exts:
                try:
                    os.remove(exts[".mp3"])
                    print(f"  [RM] {exts['.mp3']}")
                    removed += 1
                except OSError as e:
                    print(f"  [WARN] {e}")
    print(f"[DEDUPE] Removed {removed} redundant MP3(s).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Authenticated CHILDES downloader")
    ap.add_argument("--username",      required=True)
    ap.add_argument("--password",      required=True)
    ap.add_argument("--targets-file",  default=TARGETS_FILE,
                    help=f"Newline-separated URL file (default: {TARGETS_FILE})")
    ap.add_argument("--target",        action="append",
                    help="Extra URL (repeatable, appended after targets-file)")
    ap.add_argument("--download-dir",  default=DEFAULT_DIR)
    ap.add_argument("--metadata-file", default=METADATA_FILE,
                    help=f"TSV file to append per-hub summaries (default: {METADATA_FILE})")
    ap.add_argument("--no-dedupe",     action="store_true")
    ap.add_argument("--show-browser",  action="store_true")
    args = ap.parse_args()

    # --- Load targets ---
    targets = []
    tf = Path(args.targets_file)
    if tf.exists():
        lines = [l.strip() for l in tf.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.strip().startswith("#")]
        targets += lines
        print(f"[INFO] Loaded {len(lines)} URL(s) from {tf}")
    else:
        print(f"[WARN] targets file not found: {tf}")
    if args.target:
        targets += args.target

    if not targets:
        raise SystemExit(
            f"[FATAL] No targets found. Create {TARGETS_FILE} with one URL per line."
        )

    dl_dir = os.path.abspath(args.download_dir)
    os.makedirs(dl_dir, exist_ok=True)
    meta_file = os.path.abspath(args.metadata_file)

    print(f"Download dir  : {dl_dir}")
    print(f"Metadata file : {meta_file}")
    print(f"Targets       : {len(targets)}")
    print(f"File types    : {', '.join(sorted(TARGET_EXTENSIONS))}\n")

    cookies = browser_login(args.username, args.password, headless=not args.show_browser)

    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=MEDIA_HOST)

    visited: set = set()
    grand_total = 0

    for i, url in enumerate(targets, 1):
        norm_url   = normalise(url)
        local_dir  = url_to_local_path(norm_url, dl_dir)
        print(f"\n{'='*60}")
        print(f"Hub {i}/{len(targets)}: {url}")
        print(f"Local dir   : {local_dir}")
        print(f"{'='*60}")

        t0 = time.time()
        downloaded, skipped = crawl(session, url, dl_dir, visited)
        elapsed = time.time() - t0

        grand_total += downloaded

        # --- Per-hub summary ---
        stats = folder_stats(local_dir) if os.path.isdir(local_dir) else {}
        wav_info = stats.get("by_ext", {}).get(".wav", {})
        mp3_info = stats.get("by_ext", {}).get(".mp3", {})
        cha_info = stats.get("by_ext", {}).get(".cha", {})

        print(f"\n[SUMMARY] Hub {i}: {url}")
        print(f"  New downloads   : {downloaded}")
        print(f"  Skipped (exist) : {skipped}")
        print(f"  Total files     : {stats.get('total_files', 0)}")
        print(f"  Total size      : {fmt_size(stats.get('total_size_bytes', 0))}")
        print(f"  Total duration  : {fmt_duration(stats.get('total_duration_seconds', 0))}")
        print(f"  WAV  : {wav_info.get('files',0)} files, "
              f"{fmt_size(wav_info.get('size_bytes',0))}, "
              f"{fmt_duration(wav_info.get('duration_seconds',0))}")
        print(f"  MP3  : {mp3_info.get('files',0)} files, "
              f"{fmt_size(mp3_info.get('size_bytes',0))}, "
              f"{fmt_duration(mp3_info.get('duration_seconds',0))}")
        print(f"  CHA  : {cha_info.get('files',0)} files, "
              f"{fmt_size(cha_info.get('size_bytes',0))}")
        print(f"  Elapsed         : {fmt_duration(elapsed)}")

        append_metadata({
            "timestamp":              datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "hub_url":                url,
            "local_dir":              local_dir,
            "new_files_downloaded":   downloaded,
            "skipped_files":          skipped,
            "total_files_in_folder":  stats.get("total_files", 0),
            "total_size":             fmt_size(stats.get("total_size_bytes", 0)),
            "total_audio_duration":   fmt_duration(stats.get("total_duration_seconds", 0)),
            "wav_files":              wav_info.get("files", 0),
            "wav_size":               fmt_size(wav_info.get("size_bytes", 0)),
            "wav_duration":           fmt_duration(wav_info.get("duration_seconds", 0)),
            "mp3_files":              mp3_info.get("files", 0),
            "mp3_size":               fmt_size(mp3_info.get("size_bytes", 0)),
            "mp3_duration":           fmt_duration(mp3_info.get("duration_seconds", 0)),
            "cha_files":              cha_info.get("files", 0),
            "cha_size":               fmt_size(cha_info.get("size_bytes", 0)),
            "download_elapsed":       fmt_duration(elapsed),
        }, meta_file)

    if not args.no_dedupe:
        dedupe(dl_dir)

    print(f"\n✓ All done. {grand_total} new file(s) downloaded to: {dl_dir}")
    print(f"  Metadata log : {meta_file}")


if __name__ == "__main__":
    main()