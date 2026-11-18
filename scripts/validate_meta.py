#!/usr/bin/env python3
"""Read-only validation of Praise & Peace Meta publishing credentials.

This script never creates or publishes content. It verifies that each token can
read the expected Facebook Page or Instagram professional account.
"""
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


def graph_get(path: str, token: str, fields: str) -> dict:
    query = urllib.parse.urlencode({"fields": fields})
    url = f"{GRAPH_BASE}/{path.lstrip('/')}?{query}"
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "praise-peace-meta-validator/1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            error = body.get("error", {})
            message = str(error.get("message", "Meta API request failed"))
            message = message.replace(token, "[REDACTED]")
            code = error.get("code", "unknown")
            subcode = error.get("error_subcode")
            details = f" (code={code}"
            if subcode is not None:
                details += f", subcode={subcode}"
            details += ")"
        except Exception:
            message = "Meta API returned an unreadable error response"
            details = ""
        raise MetaApiError(f"HTTP {exc.code}: {message}{details}") from None
    except urllib.error.URLError:
        raise MetaApiError("Network error while contacting Meta Graph API") from None
    except TimeoutError:
        raise MetaApiError("Meta Graph API request timed out") from None

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise MetaApiError("Meta Graph API returned invalid JSON") from None

    if "error" in data:
        error = data["error"]
        message = str(error.get("message", "Meta API request failed"))
        message = message.replace(token, "[REDACTED]")
        raise MetaApiError(
            f"{message} (code={error.get('code', 'unknown')})"
        )

    return data


def require_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise MetaApiError(f"GitHub Actions secret is missing: {name}")
    return value


def main() -> int:
    try:
        facebook_token = require_secret("META_FACEBOOK_PAGE_ACCESS_TOKEN")
        instagram_token = require_secret("META_INSTAGRAM_ACCESS_TOKEN")

        page = graph_get(
            FACEBOOK_PAGE_ID,
            facebook_token,
            "id,name",
        )
        if page.get("id") != FACEBOOK_PAGE_ID:
            raise MetaApiError("Facebook token returned an unexpected Page ID")
        if page.get("name") != EXPECTED_PAGE_NAME:
            raise MetaApiError(
                f"Facebook token resolved to an unexpected Page name: {page.get('name', 'unknown')}"
            )
        print(f"PASS: Facebook Page token verified for {page['name']} ({page['id']}).")

        instagram = graph_get(
            INSTAGRAM_USER_ID,
            instagram_token,
            "id,username",
        )
        if instagram.get("id") != INSTAGRAM_USER_ID:
            raise MetaApiError("Instagram token returned an unexpected account ID")
        username = instagram.get("username", "")
        if username.lower() != EXPECTED_IG_USERNAME:
            raise MetaApiError(
                f"Instagram token resolved to an unexpected username: @{username or 'unknown'}"
            )
        print(f"PASS: Instagram token verified for @{username} ({instagram['id']}).")

        print("PASS: Both Meta credentials can read the expected accounts.")
        print("READ-ONLY CHECK COMPLETE: No content was created or published.")
        return 0

    except MetaApiError as exc:
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
