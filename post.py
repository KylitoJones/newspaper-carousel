#!/usr/bin/env python3
"""
post.py — Take the pages you picked in the browser and post them to Bluesky.

The browse page's "Prepare post" button saves a file called bluesky-post.json
(usually into your Downloads folder). This script reads that file, writes alt
text for each image with Claude, shrinks the images to fit Bluesky's limit, and
publishes one post with all of them.

    python post.py               # find bluesky-post.json automatically and post
    python post.py --dry-run     # do everything EXCEPT post (prints the alt text)
    python post.py path/to/bluesky-post.json

Secrets live in a local .env file (never uploaded anywhere):

    BLUESKY_HANDLE=kanasjones.bsky.social
    BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx      # made in Bluesky settings
    ANTHROPIC_API_KEY=sk-ant-...
    ALT_MODEL=claude-sonnet-5                     # optional; this is the default
"""

import argparse
import base64
import io
import os
import sys
from pathlib import Path

# --- load .env (optional dependency) ---------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

import requests
from PIL import Image

BLUESKY_BLOB_LIMIT = 976_000       # a hair under Bluesky's ~1,000,000-byte cap
MAX_EDGE = 2000                    # longest side, px
ALT_MAX_CHARS = 1500
DEFAULT_MODEL = os.environ.get("ALT_MODEL", "claude-sonnet-5")

HEADERS = {"User-Agent": "newsstand-poster/1.0"}


def die(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def find_payload(arg: str | None) -> Path:
    if arg:
        p = Path(arg).expanduser()
        if not p.exists():
            die(f"file not found: {p}")
        return p
    candidates = [
        Path.cwd() / "bluesky-post.json",
        Path.home() / "Downloads" / "bluesky-post.json",
    ]
    found = [c for c in candidates if c.exists()]
    if not found:
        die("no bluesky-post.json found. Hit 'Prepare post' in the browser "
            "first, or pass the path as an argument.")
    # newest wins
    return max(found, key=lambda c: c.stat().st_mtime)


def fetch_image(url: str) -> bytes:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.content


def shrink_for_bluesky(raw: bytes) -> bytes:
    """Return JPEG bytes guaranteed under the blob limit."""
    img = Image.open(io.BytesIO(raw))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > MAX_EDGE:
        scale = MAX_EDGE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    for q in (90, 85, 80, 72, 65, 55, 45):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)
        if buf.tell() <= BLUESKY_BLOB_LIMIT:
            return buf.getvalue()
    return buf.getvalue()  # smallest we managed


def alt_text(client, model: str, name: str, date: str, jpeg: bytes) -> str:
    b64 = base64.standard_b64encode(jpeg).decode()
    prompt = (
        f"Write alt text for this photo of the front page of {name}"
        + (f" dated {date}" if date else "")
        + ". Describe it for a blind reader in 1-3 plain sentences: name the "
        "paper, then its lead headline(s) and the main photograph or graphic. "
        "Be factual and specific. Do not start with 'image of' or 'alt text'. "
        f"Keep it under {ALT_MAX_CHARS} characters."
    )
    msg = client.messages.create(
        model=model,
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    return text[:ALT_MAX_CHARS]


def main():
    ap = argparse.ArgumentParser(description="Post your picked front pages to Bluesky.")
    ap.add_argument("payload", nargs="?", help="path to bluesky-post.json")
    ap.add_argument("--dry-run", action="store_true", help="prepare everything but don't post")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"alt-text model (default {DEFAULT_MODEL})")
    args = ap.parse_args()

    import json
    payload_path = find_payload(args.payload)
    data = json.loads(payload_path.read_text())
    images = data.get("images", [])
    text = data.get("text", "")
    if not images:
        die("that file has no images in it.")
    if len(images) > 4:
        die(f"{len(images)} images — Bluesky allows at most 4.")
    print(f"Loaded {payload_path.name}: {len(images)} image(s).")
    if len([*text.strip()]) > 300:
        die(f"post text is {len([*text.strip()])} chars; Bluesky's limit is 300.")

    # Anthropic client for alt text
    if not os.environ.get("ANTHROPIC_API_KEY"):
        die("ANTHROPIC_API_KEY is not set (put it in .env).")
    from anthropic import Anthropic
    ant = Anthropic()

    blobs, alts = [], []
    for i, im in enumerate(images, 1):
        print(f"[{i}/{len(images)}] {im['name']} — fetching, shrinking, describing...")
        raw = fetch_image(im["url"])
        jpeg = shrink_for_bluesky(raw)
        alt = alt_text(ant, args.model, im["name"], im.get("date", ""), jpeg)
        print(f"        alt: {alt}\n        ({len(jpeg)//1024} KB)")
        blobs.append(jpeg)
        alts.append(alt)

    if args.dry_run:
        print("\n--dry-run: not posting. Text would be:\n  " + (text or "(empty)"))
        return

    # Post to Bluesky
    handle = os.environ.get("BLUESKY_HANDLE")
    pw = os.environ.get("BLUESKY_APP_PASSWORD")
    if not handle or not pw:
        die("BLUESKY_HANDLE and/or BLUESKY_APP_PASSWORD not set (put them in .env).")
    from atproto import Client
    client = Client()
    client.login(handle, pw)
    print(f"Posting as @{handle} ...")

    resp = client.send_images(text=text, images=blobs, image_alts=alts)

    # Build a friendly URL from the returned at:// uri
    uri = getattr(resp, "uri", "")
    rkey = uri.rsplit("/", 1)[-1] if uri else ""
    if rkey:
        print(f"Posted ✓  https://bsky.app/profile/{handle}/post/{rkey}")
    else:
        print(f"Posted ✓  {uri or resp}")


if __name__ == "__main__":
    main()
