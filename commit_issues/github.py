"""The only GitHub integration is the official Sprites Connector gateway."""

import json
import re
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

GATEWAY = "https://api.sprites.dev/v1/gateway/github"
LABEL = "commit-issues-demo"
MAX_FILES = 12
COMMENT_PAGE_SIZE = 100
REPO_PATTERN = r"[a-zA-Z0-9][a-zA-Z0-9-]{0,38}/[a-zA-Z0-9_.-]{1,100}"
SHA_PATTERN = r"[0-9a-f]{40}"


class AppError(Exception):
    pass


class HTTPStatusError(AppError):
    def __init__(self, status, retry_after=None):
        super().__init__(f"Request rejected or interrupted (HTTP {status}).")
        self.status = status
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(method, url, *, headers, body=None, expected=200, max_bytes=1_500_000):
    data = None if body is None else json.dumps(body, ensure_ascii=True).encode()
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(request, timeout=18) as response:
            if response.status != expected:
                raise AppError(f"Unexpected HTTP status {response.status}.")
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise AppError("Response exceeded the size limit.")
    except HTTPError as error:
        retry_after = error.headers.get("Retry-After") if error.headers is not None else None
        error.close()
        raise HTTPStatusError(error.code, retry_after) from None
    except (OSError, URLError, HTTPException):
        raise AppError("Request did not complete. Check the connection and configured permissions.") from None
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise AppError("Invalid JSON from the upstream service.") from None


def repo_name(value):
    if not isinstance(value, str) or not re.fullmatch(REPO_PATTERN, value):
        raise AppError("Set one designated GitHub repository in OWNER/REPO form.")
    if value.split("/")[1] in {".", ".."}:
        raise AppError("Invalid repository name.")
    return value.lower()


class GitHub:
    headers = {
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "commit-issues-v0",
    }

    def __init__(self, repo, gateway=GATEWAY, send=request_json):
        self.repo = repo_name(repo)
        if not re.fullmatch(re.escape(GATEWAY) + r"(?:/[A-Za-z0-9_-]{1,128})?/?", gateway):
            raise AppError("Use the official GitHub Sprites Connector gateway URL.")
        self.root = f"{gateway.rstrip('/')}/repos/{self.repo}"
        self.send = send

    def target(self, number):
        if type(number) is not int or not 1 <= number <= 2_147_483_647:
            raise AppError("Specify a valid pull request number.")
        pr = self.send("GET", f"{self.root}/pulls/{number}", headers=self.headers)
        try:
            matches = (
                pr["number"] == number and pr["state"] == "open" and pr["draft"] is False
                and pr["base"]["repo"]["full_name"].lower() == self.repo
                and pr["head"]["repo"]["full_name"].lower() == self.repo
                and pr["user"]["type"] == "User"
                and not pr["user"]["login"].lower().endswith("[bot]")
                and any(label["name"] == LABEL for label in pr["labels"])
                and re.fullmatch(SHA_PATTERN, pr["head"]["sha"])
                and re.fullmatch(SHA_PATTERN, pr["base"]["sha"])
                and type(pr["changed_files"]) is int and pr["changed_files"] > 0
            )
        except (KeyError, AttributeError, TypeError):
            matches = False
        if not matches:
            raise AppError(f"Use an open, non-draft, human-authored, same-repository PR labeled {LABEL}.")
        return {"repo": self.repo, "number": number, "head": pr["head"]["sha"],
                "base": pr["base"]["sha"], "changed_files": pr["changed_files"]}

    def files(self, number):
        rows = self.send("GET", f"{self.root}/pulls/{number}/files?per_page={MAX_FILES}",
                         headers=self.headers)
        if not isinstance(rows, list) or not rows or len(rows) > MAX_FILES:
            raise AppError("The Connector did not return a bounded list of changed files.")
        return rows

    def comments(self, number):
        url = f"{self.root}/issues/{number}/comments?per_page={COMMENT_PAGE_SIZE}"
        rows = self.send("GET", url, headers=self.headers)
        if not isinstance(rows, list) or len(rows) >= COMMENT_PAGE_SIZE:
            raise AppError("Could not verify the full comment history within the 99-comment limit.")
        if any(not isinstance(row, dict) or not isinstance(row.get("body"), str) for row in rows):
            raise AppError("Invalid comment history from the Connector.")
        return rows

    def post(self, number, body):
        result = self.send("POST", f"{self.root}/issues/{number}/comments", headers=self.headers,
                           body={"body": body}, expected=201)
        if not isinstance(result, dict) or type(result.get("id")) is not int or result["id"] < 1:
            raise AppError("The Connector did not return a valid comment ID.")
        return result["id"]
