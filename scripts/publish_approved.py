#!/usr/bin/env python3
"""Publish an explicitly approved Praise and Peace post. Never auto-generate copy."""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://graph.facebook.com/v27.0"
REPO = "velonieb-droid/praise-peace-automation"
FB_PAGE = "879577415240432"
IG_USER = "17841479478148076"


class Blocked(Exception):
    pass


def need(name):
    val = os.environ.get(name, "").strip()
    if not val:
        raise Blocked("Missing environment variable or secret: " + name)
    return val


def call(method, path, token, args=None):
    base = API_BASE + "/" + path.lstrip("/")
    encoded = urllib.parse.urlencode(args or {}).encode("utf-8")
    url = base + ("?" + encoded.decode("utf-8") if method == "GET" and encoded else "")
    req = urllib.request.Request(
        url, data=encoded if method == "POST" else None, method=method,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read().decode("utf-8")).get("error", {})
            code = err.get("code", "unknown")
            subcode = err.get("error_subcode", "none")
        except (ValueError, TypeError):
            code, subcode = "unknown", "none"
        # Never log provider response bodies or URL query parameters; they may contain secrets.
        raise Blocked(f"Meta {method} {path}: HTTP {exc.code}, code={code}, subcode={subcode}") from None
    except (urllib.error.URLError, TimeoutError):
        raise Blocked(f"Meta {method} {path}: network error; outcome uncertain. Check platform before retrying.") from None
    except (ValueError, TypeError):
        raise Blocked(f"Meta {method} {path}: invalid JSON") from None
    if not isinstance(result, dict) or "error" in result:
        raise Blocked(f"Meta {method} {path}: returned unexpected/error data")
    return result


def validate(post_id):
    if not re.fullmatch(r"PP-\d{4}-\d{2}-\d{2}", post_id):
        raise Blocked("Invalid post ID")
    path = ROOT / "posts" / (post_id + ".json")
    if not path.is_file():
        raise Blocked("Approved post manifest missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (data.get("post_id") != post_id or data.get("status") != "approved"
            or data.get("approval_phrase") != "APPROVED " + post_id):
        raise Blocked("No valid explicit approval for this release")
    if set(data.get("approved_platforms", [])) != {"facebook", "instagram"}:
        raise Blocked("Both platforms require explicit approval")
    if data.get("platform_ids") != {
        "facebook_page": FB_PAGE, "instagram_user": IG_USER,
    }:
        raise Blocked("Target identity differs from approval")
    caption = data.get("caption", "")
    if not isinstance(caption, str) or not 100 <= len(caption) <= 2100:
        raise Blocked("Approved caption is missing or invalid")
    image = data.get("image", {})
    path = ROOT / str(image.get("path", ""))
    if (not path.is_file() or path.resolve().parent != (ROOT / "images").resolve()
            or path.suffix.lower() != ".jpg"):
        raise Blocked("Exact approved JPEG file must be in images/")
    contents = path.read_bytes()
    if not contents.startswith(b"\xff\xd8\xff"):
        raise Blocked("Image has invalid JPEG header")
    if hashlib.sha256(contents).hexdigest() != image.get("sha256"):
        raise Blocked("Image SHA-256 does not match approved asset")
    if (image.get("width"), image.get("height")) != (1080, 1080):
        raise Blocked("Image metadata must be 1080 by 1080")
    return data, path


def public_image_check(url, expected):
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "PraisePeacePublisher/1.0"}),
            timeout=40,
        ) as response:
            mime = response.headers.get("Content-Type", "")
            contents = response.read(6_000_000)
    except (urllib.error.URLError, TimeoutError):
        raise Blocked("Public image not accessible to Meta") from None
    if "image/jpeg" not in mime.lower() or hashlib.sha256(contents).hexdigest() != expected:
        raise Blocked("Public image MIME or checksum does not match approved image")


def recent_duplicate(platform, token, caption):
    ident = FB_PAGE if platform == "facebook" else IG_USER
    edge = "posts" if platform == "facebook" else "media"
    fields = "id,message,permalink_url" if platform == "facebook" else "id,caption,permalink"
    after = None
    for _ in range(12):
        opts = {"fields": fields, "limit": "50"}
        if after:
            opts["after"] = after
        payload = call("GET", f"{ident}/{edge}", token, opts)
        for record in payload.get("data", []):
            body = record.get("message", record.get("caption", ""))
            if body.strip() == caption.strip():
                return {
                    "status": "published", "id": str(record["id"]),
                    "url": record.get("permalink_url", record.get("permalink", "")),
                    "source": "duplicate_check",
                }
        if not payload.get("paging", {}).get("next"):
            return None
        cursor = payload.get("paging", {}).get("cursors", {}).get("after")
        if not cursor or cursor == after:
            raise Blocked(f"{platform}: pagination did not advance; manual verification needed")
        after = cursor
    raise Blocked(f"{platform}: duplicate scan exceeded 600 entries; manual verification needed")


def save_state(post_id, state):
    path = ROOT / "state" / (post_id + ".json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    def git(args):
        subprocess.run(["git"] + args, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    git(["add", str(path.relative_to(ROOT))])
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    git([
        "-c", "user.name=github-actions[bot]",
        "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-m", "Record publishing status for " + post_id,
    ])
    try:
        git(["push", "origin", "HEAD:main"])
    except subprocess.CalledProcessError:
        raise Blocked("Could not persist platform status. Inspect live posts before retry.") from None


def main():
    try:
        post_id = os.environ.get("POST_ID", "PP-2026-11-19")
        manifest, path = validate(post_id)
        fb_token = need("META_FACEBOOK_PAGE_ACCESS_TOKEN")
        ig_token = need("META_INSTAGRAM_ACCESS_TOKEN")
        sha = need("GITHUB_SHA")
        public_url = f"https://raw.githubusercontent.com/{REPO}/{sha}/{path.relative_to(ROOT).as_posix()}"
        public_image_check(public_url, manifest["image"]["sha256"])

        fb = call("GET", FB_PAGE, fb_token, {"fields": "id,name"})
        if fb.get("id") != FB_PAGE or fb.get("name") != "Praise and Peace":
            raise Blocked("Facebook Page identity check failed")
        ig = call("GET", IG_USER, ig_token, {"fields": "id,username"})
        if ig.get("id") != IG_USER or str(ig.get("username", "")).lower() != "praisenpeace":
            raise Blocked("Instagram identity check failed")
        print("PASS: approval, image checksum, public image URL, Page and Instagram identities")

        status_path = ROOT / "state" / (post_id + ".json")
        state = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {
            "post_id": post_id, "facebook": {"status": "pending"},
            "instagram": {"status": "pending"},
        }
        caption = manifest["caption"]
        for platform in ("facebook", "instagram"):
            if state.get(platform, {}).get("status") == "published":
                print("SKIP: already published to " + platform)
                continue
            token = fb_token if platform == "facebook" else ig_token
            old = recent_duplicate(platform, token, caption)
            if old:
                state[platform] = old
                save_state(post_id, state)
                print("SKIP: matching approved caption already on " + platform)
                continue

            print("Publishing approved release to " + platform)
            if platform == "facebook":
                result = call("POST", f"{FB_PAGE}/photos", fb_token, {
                    "url": public_url, "caption": caption, "published": "true",
                })
                media_id = result.get("post_id") or result.get("id")
                if not media_id:
                    raise Blocked("Facebook result has no media ID; inspect before retry")
                url = "https://www.facebook.com/" + str(media_id)
            else:
                container = call("POST", f"{IG_USER}/media", ig_token, {
                    "image_url": public_url, "caption": caption, "is_ai_generated": "true",
                    "alt_text": manifest["image"].get("alt_text", ""),
                })
                cid = container.get("id")
                if not cid:
                    raise Blocked("Instagram did not return a media container ID")
                for _ in range(8):
                    status = call("GET", str(cid), ig_token, {"fields": "status_code"}).get("status_code")
                    if status == "FINISHED":
                        break
                    if status in ("ERROR", "EXPIRED"):
                        raise Blocked("Instagram image preparation failed: " + status)
                    time.sleep(5)
                else:
                    raise Blocked("Instagram media container did not finish; no publish sent")
                result = call("POST", f"{IG_USER}/media_publish", ig_token, {"creation_id": str(cid)})
                media_id = result.get("id")
                if not media_id:
                    raise Blocked("Instagram publish returned no media ID; inspect before retry")
                url = call("GET", str(media_id), ig_token, {"fields": "permalink"}).get("permalink", "")

            state[platform] = {"status": "published", "id": str(media_id), "url": url}
            save_state(post_id, state)
            print("PASS: " + platform + " published: " + url)
        print("Complete. Platform statuses are in state/" + post_id + ".json")
        return 0
    except Blocked as exc:
        print("BLOCKED: " + str(exc), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        print("BLOCKED: Git failed; no further platform publishing attempted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
