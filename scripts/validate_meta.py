#!/usr/bin/env python3
"""Read-only validation of the Meta system-user token and assigned social assets."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v27.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
FACEBOOK_PAGE_ID = "879577415240432"
EXPECTED_PAGE_NAME = "Praise and Peace"
INSTAGRAM_USER_ID = "17841479478148076"
EXPECTED_IG_USERNAME = "praisenpeace"


class MetaApiError(RuntimeError):
    pass


def graph_get(path: str, token: str, fields: str | None = None, extra: dict | None = None) -> dict:
    params = dict(extra or {})
    if fields:
        params["fields"] = fields
    query = urllib.parse.urlencode(params)
    url = f"{GRAPH_BASE}/{path.lstrip('/')}" + (f"?{query}" if query else "")
    request = urllib.request.Request(
        url, method="GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "User-Agent": "praise-peace-meta-validator/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read().decode("utf-8")).get("error", {})
            code = error.get("code", "unknown")
            subcode = error.get("error_subcode")
            details = f" (code={code}" + (f", subcode={subcode}" if subcode is not None else "") + ")"
        except Exception:
            details = ""
        raise MetaApiError(f"HTTP {exc.code}{details}") from None
    except (urllib.error.URLError, TimeoutError):
        raise MetaApiError("Network error contacting Meta Graph API") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise MetaApiError("Meta Graph API returned invalid JSON") from None
    if not isinstance(data, dict) or "error" in data:
        raise MetaApiError("Meta Graph API returned an error")
    return data


def require_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise MetaApiError(f"GitHub Actions secret is missing: {name}")
    return value


def main() -> int:
    try:
        system_token = require_secret("META_SYSTEM_USER_ACCESS_TOKEN")

        pages = graph_get(
            "me/accounts", system_token,
            "id,name,access_token,tasks,instagram_business_account",
            {"limit": "100"},
        )
        page = next(
            (item for item in pages.get("data", [])
             if item.get("id") == FACEBOOK_PAGE_ID and item.get("name") == EXPECTED_PAGE_NAME),
            None,
        )
        if not page or not page.get("access_token"):
            raise MetaApiError("System-user token cannot retrieve the expected Facebook Page access token")
        page_token = page["access_token"]
        tasks = page.get("tasks", [])
        if tasks and not ({"CREATE_CONTENT", "MANAGE"} & set(tasks)):
            raise MetaApiError("System user lacks the Facebook Page content-creation task")

        verified_page = graph_get(FACEBOOK_PAGE_ID, page_token, "id,name")
        if verified_page.get("id") != FACEBOOK_PAGE_ID or verified_page.get("name") != EXPECTED_PAGE_NAME:
            raise MetaApiError("Resolved Facebook Page token returned an unexpected Page identity")
        print(f"PASS: system-user token resolved Facebook Page {EXPECTED_PAGE_NAME} ({FACEBOOK_PAGE_ID}).")

        ig = None
        ig_auth_method = None
        for label, candidate in (("system-user token", system_token), ("Page token", page_token)):
            try:
                result = graph_get(INSTAGRAM_USER_ID, candidate, "id,username")
                if result.get("id") == INSTAGRAM_USER_ID and str(result.get("username", "")).lower() == EXPECTED_IG_USERNAME:
                    ig = result
                    ig_auth_method = label
                    break
            except MetaApiError:
                continue
        if not ig:
            raise MetaApiError("Neither system-user nor linked Page token can verify the expected Instagram account")

        print(f"PASS: Instagram account @{ig['username']} ({ig['id']}) verified using {ig_auth_method}.")
        print("READ-ONLY CHECK COMPLETE: No content was created or published.")
        return 0
    except MetaApiError as exc:
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
