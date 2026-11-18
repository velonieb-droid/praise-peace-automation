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
            error_type = re.sub(r"[^A-Za-z0-9_.-]", "", str(err.get("type", "unknown")))[:60]
            transient = str(err.get("is_transient", "unknown")).lower()
            trace_id = re.sub(r"[^A-Za-z0-9]", "", str(err.get("fbtrace_id", "")))[:80] or "none"
            message = str(err.get("message", ""))
            message = re.sub(r"https?://[^ ]+", "[URL]", message)
            message = message.replace(token, "[REDACTED_TOKEN]")
            message = message.replace(chr(13), " ").replace(chr(10), " ").replace(chr(9), " ").strip()[:240]
        except (ValueError, TypeError, AttributeError):
            code, subcode = "unknown", "none"
            error_type, transient, trace_id, message = "unknown", "unknown", "none", ""
        # Log only bounded diagnostic metadata; never log tokens, request bodies, or URL query strings.
        raise Blocked(
            f"Meta {method} {path}: HTTP {exc.code}, code={code}, subcode={subcode}, "
            f"type={error_type}, transient={transient}, fbtrace_id={trace_id}, "
            f"message={message or 'unavailable'}"
        ) from None
    except (urllib.error.URLError, TimeoutError):
        raise Blocked(f"Meta {method} {path}: network error; outcome uncertain. Check platform before retrying.") from None
    except (ValueError, TypeError):
        raise Blocked(f"Meta {method} {path}: invalid JSON") from None
    if not isinstance(result, dict) or "error" in result:
        raise Blocked(f"Meta {method} {path}: returned unexpected/error data")
    return result


def resolve_platform_tokens(system_token):
    """Resolve a Page token from the system user and test the linked IG account."""
    pages = call("GET", "me/accounts", system_token, {
        "fields": "id,name,access_token,tasks,instagram_business_account",
        "limit": "100",
    })
    page = next(
        (item for item in pages.get("data", [])
         if item.get("id") == FB_PAGE and item.get("name") == "Praise and Peace"),
        None,
    )
    if not page or not page.get("access_token"):
        raise Blocked("System-user token cannot retrieve the assigned Facebook Page access token")
    page_token = page["access_token"]
    tasks = page.get("tasks", [])
    if tasks and not ({"CREATE_CONTENT", "MANAGE"} & set(tasks)):
        raise Blocked("System user lacks Facebook Page content-creation permission")

    ig_token = None
    for candidate in (system_token, page_token):
        try:
            ig = call("GET", IG_USER, candidate, {"fields": "id,username"})
        except Blocked:
            continue
        if ig.get("id") == IG_USER and str(ig.get("username", "")).lower() == "praisenpeace":
            ig_token = candidate
            break
    if not ig_token:
        raise Blocked("System-user token and linked Page token could not verify the assigned Instagram account")
    return page_token, ig_token


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
    if hashlib.sha256(caption.encode("utf-8")).hexdigest() != data.get("caption_sha256"):
        raise Blocked("Caption has changed since approval; publishing requires new approval")
    image = data.get("image", {})
    if not re.fullmatch(r"[A-Za-z0-9_-]{15,}", str(image.get("drive_file_id", ""))):
        raise Blocked("Approved Google Drive file ID is missing or invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(image.get("sha256", ""))):
        raise Blocked("Approved image SHA-256 is missing or invalid")
    if (image.get("width"), image.get("height")) != (1080, 1080):
        raise Blocked("Approved image metadata must be 1080 by 1080")
    public_url = str(image.get("public_delivery_url") or "").strip()
    if not public_url:
        raise Blocked("Public direct JPEG delivery URL is not configured or verified")
    parsed = urllib.parse.urlsplit(public_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise Blocked("Public image delivery URL must use safe public HTTPS")
    # Both FB and Instagram fetch the image anonymously. Never use a Drive preview page.
    return data, public_url


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
    """Check the newest 50 items only; the durable release ledger is the primary idempotency guard.

    Do not walk the entire Page/account archive: Meta limits and ranks historical
    Page results, and an unbounded scan can block a valid first-time publication.
    A just-published item (including a publish whose response was interrupted)
    should be among the newest 50 when this workflow is retried.
    """
    ident = FB_PAGE if platform == "facebook" else IG_USER
    edge = "posts" if platform == "facebook" else "media"
    fields = (
        "id,message,created_time,permalink_url"
        if platform == "facebook"
        else "id,caption,timestamp,permalink"
    )
    payload = call("GET", f"{ident}/{edge}", token, {"fields": fields, "limit": "50"})
    records = payload.get("data")
    if not isinstance(records, list):
        raise Blocked(f"{platform}: duplicate check returned an invalid data set")
    for record in records:
        body = record.get("message", record.get("caption", ""))
        if isinstance(body, str) and body.strip() == caption.strip():
            return {
                "status": "published", "id": str(record["id"]),
                "url": record.get("permalink_url", record.get("permalink", "")),
                "source": "recent_duplicate_check",
                "created_time": record.get("created_time", record.get("timestamp", "")),
            }
    return None


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
        manifest, public_url = validate(post_id)
        system_token = need("META_SYSTEM_USER_ACCESS_TOKEN")
        fb_token, ig_token = resolve_platform_tokens(system_token)
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
