#!/usr/bin/env python3
"""Minimal X API v2 client for @ThemeMISELL (stdlib only, OAuth 1.0a user context).

Credentials (never in git), first match wins:
  1. env X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET (GitHub Actions secrets)
  2. ~/.config/sugoi-growth/x-oauth1.json  {"api_key","api_secret","access_token","access_secret"}
     (override path with GROWTH_X_JSON)

Pricing is pay-per-use (docs.x.com/x-api/getting-started/pricing, checked 2026-10-19):
owned reads $0.001/resource, Post: Create $0.015, Post: Create (with URL) $0.200.
Every write goes through WriteBudget so a bug cannot burn credits in a loop.

CLI:  python3 x_api.py me      -> prints the authenticated account (read only)
"""
import base64, hashlib, hmac, json, os, secrets, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

API = "https://api.x.com/2"
EXPECTED_USERNAME = "ThemeMISELL"
CRED_PATH = Path(os.environ.get("GROWTH_X_JSON", str(Path.home() / ".config/sugoi-growth/x-oauth1.json")))


class XError(RuntimeError):
    pass


class WriteBudget:
    """Hard cap on write calls per process (posts + replies + uploads)."""

    def __init__(self, limit: int):
        self.limit, self.used = limit, 0

    def spend(self, what: str):
        if self.used >= self.limit:
            raise XError(f"write budget exhausted ({self.limit}) before: {what}")
        self.used += 1


def load_credentials() -> dict:
    env = {k: os.environ.get(f"X_{k.upper()}") for k in ("api_key", "api_secret", "access_token", "access_secret")}
    if all(env.values()):
        return env
    if CRED_PATH.exists():
        cred = json.loads(CRED_PATH.read_text())
        missing = [k for k in env if not cred.get(k)]
        if missing:
            raise XError(f"{CRED_PATH} is missing {missing}")
        return cred
    raise XError(f"no X credentials: set X_API_KEY etc. or create {CRED_PATH}")


def _pct(s: str) -> str:
    return urllib.parse.quote(str(s), safe="~-._")


def oauth1_header(method: str, url: str, cred: dict, query: dict | None = None) -> str:
    """OAuth 1.0a HMAC-SHA1. JSON bodies are not part of the signature base string."""
    oauth = {
        "oauth_consumer_key": cred["api_key"],
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": cred["access_token"],
        "oauth_version": "1.0",
    }
    params = {**(query or {}), **oauth}
    param_str = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(params.items()))
    base = "&".join([method.upper(), _pct(url), _pct(param_str)])
    key = f"{_pct(cred['api_secret'])}&{_pct(cred['access_secret'])}"
    oauth["oauth_signature"] = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


class XClient:
    def __init__(self, cred: dict | None = None, write_limit: int = 0):
        self.cred = cred or load_credentials()
        self.budget = WriteBudget(write_limit)
        self._me = None

    def _call(self, method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
        url = f"{API}{path}"
        full = url + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Authorization": oauth1_header(method, url, self.cred, query)}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(full, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise XError(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:500]}") from None

    # --- reads -------------------------------------------------------------
    def me(self) -> dict:
        if self._me is None:
            self._me = self._call("GET", "/users/me")["data"]
            if self._me.get("username") != EXPECTED_USERNAME:
                raise XError(f"authenticated as @{self._me.get('username')}, expected @{EXPECTED_USERNAME}")
        return self._me

    def my_posts(self, start_time: str, max_pages: int = 5) -> list[dict]:
        """Own posts since start_time (RFC3339). Owned read: $0.001 per post returned.
        non_public/organic metrics exist only for posts younger than 30 days."""
        uid, out, token = self.me()["id"], [], None
        for _ in range(max_pages):
            q = {
                "start_time": start_time,
                "max_results": "100",
                "exclude": "retweets",
                "tweet.fields": "created_at,public_metrics,non_public_metrics,organic_metrics,conversation_id,in_reply_to_user_id,attachments",
            }
            if token:
                q["pagination_token"] = token
            res = self._call("GET", f"/users/{uid}/tweets", query=q)
            out.extend(res.get("data", []))
            token = res.get("meta", {}).get("next_token")
            if not token:
                break
        return out

    def liking_users(self, post_id: str, max_pages: int = 1) -> list[dict]:
        """Users who liked a post. Owned read. The API caps history at 100 users per post for all time."""
        out, token = [], None
        for _ in range(max_pages):
            q = {"max_results": "100", "user.fields": "id,username,name,description,public_metrics"}
            if token:
                q["pagination_token"] = token
            res = self._call("GET", f"/tweets/{post_id}/liking_users", query=q)
            out.extend(res.get("data", []))
            token = res.get("meta", {}).get("next_token")
            if not token:
                break
        return out

    # --- writes (each spends budget) --------------------------------------
    def follow(self, target_user_id: str) -> dict:
        """Follow a user as @ThemeMISELL. Runs only on explicit owner approval per call, never automated."""
        me = self.me()
        return self._call("POST", f"/users/{me['id']}/following", body={"target_user_id": target_user_id})

    def upload_image(self, path: str) -> str:
        self.budget.spend(f"upload {path}")
        b64 = base64.b64encode(Path(path).read_bytes()).decode()
        res = self._call("POST", "/media/upload", body={"media": b64, "media_category": "tweet_image"})
        return res["data"]["id"]

    def create_post(self, text: str, media_ids: list[str] | None = None, reply_to: str | None = None) -> str:
        self.budget.spend(f"post {text[:20]!r}")
        body: dict = {"text": text}
        if media_ids:
            body["media"] = {"media_ids": media_ids}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        return self._call("POST", "/tweets", body=body)["data"]["id"]


if __name__ == "__main__":
    if sys.argv[1:] == ["me"]:
        print(json.dumps(XClient().me(), ensure_ascii=False))
    else:
        print(__doc__)
        sys.exit(2)
