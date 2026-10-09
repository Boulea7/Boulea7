"""Refresh public, all-time external merged PR statistics with GitHub GraphQL."""

import html
import http.client
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 3
QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    pullRequests(states: MERGED, first: 100, after: $after) {
      totalCount
      nodes {
        id
        state
        author { login }
        repository {
          id
          nameWithOwner
          isPrivate
          owner { login }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


class StatisticsError(RuntimeError):
    """The API result or local profile cannot be safely updated."""


@dataclass(frozen=True)
class ContributionStats:
    merged_prs: int
    repositories: int


def run_query(token, variables):
    """Retry only transient transport failures; never expose API response bodies."""
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": variables}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
        },
        method="POST",
    )
    for attempt in range(MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = json.loads(response.read())
            if not isinstance(payload, dict):
                raise StatisticsError("GitHub returned an invalid GraphQL response.")
            return payload
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status not in (408, 429, 500, 502, 503, 504):
                raise StatisticsError(f"GitHub request failed (HTTP {status}).") from None
        except (OSError, http.client.HTTPException):
            pass
        except (ValueError, UnicodeDecodeError):
            raise StatisticsError("GitHub returned invalid JSON.") from None
        if attempt + 1 < MAX_ATTEMPTS:
            time.sleep(2 ** attempt)
    raise StatisticsError("GitHub request failed after bounded retries.")


def require_string(value, field):
    if not isinstance(value, str) or not value.strip():
        raise StatisticsError(f"GitHub response is missing a valid {field}.")
    return value


def parse_pull_request(node, login):
    """Validate a node before deciding whether it contributes to the totals."""
    if not isinstance(node, dict):
        raise StatisticsError("GitHub returned an invalid pull request node.")
    pr_id = require_string(node.get("id"), "pull request ID")
    state = node.get("state")
    if state not in ("MERGED", "OPEN", "CLOSED"):
        raise StatisticsError("GitHub returned an invalid pull request state.")
    author = node.get("author")
    if author is not None and not isinstance(author, dict):
        raise StatisticsError("GitHub returned an invalid pull request author.")
    author_login = require_string(author.get("login"), "author login") if author else None

    repository = node.get("repository")
    if not isinstance(repository, dict):
        raise StatisticsError("GitHub response is missing a pull request repository.")
    repo_id = require_string(repository.get("id"), "repository ID")
    name = require_string(repository.get("nameWithOwner"), "repository name")
    if len(name.split("/")) != 2 or not all(name.split("/")):
        raise StatisticsError("GitHub returned an invalid repository name.")
    owner = repository.get("owner")
    if not isinstance(owner, dict):
        raise StatisticsError("GitHub response is missing a repository owner.")
    owner_login = require_string(owner.get("login"), "repository owner login")
    private = repository.get("isPrivate")
    if not isinstance(private, bool):
        raise StatisticsError("GitHub response is missing repository visibility.")
    eligible = (
        state == "MERGED" and author_login is not None
        and author_login.casefold() == login.casefold()
        and not private and owner_login.casefold() != login.casefold()
    )
    return pr_id, repo_id, eligible


def fetch_statistics(login, token, *, query_runner=None):
    """Read the entire merged PR connection and reject incomplete pagination."""
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login):
        raise StatisticsError("GITHUB_LOGIN is invalid.")
    if not isinstance(token, str) or not token.strip():
        raise StatisticsError("GITHUB_TOKEN is required.")
    if query_runner is None:
        query_runner = lambda variables: run_query(token, variables)
    after = None
    cursors = set()
    seen_prs = set()
    merged_prs = set()
    repositories = set()
    expected_total = None

    while True:
        payload = query_runner({"login": login, "after": after})
        if not isinstance(payload, dict) or "errors" in payload:
            raise StatisticsError("GitHub GraphQL query failed.")
        data = payload.get("data")
        user = data.get("user") if isinstance(data, dict) else None
        connection = user.get("pullRequests") if isinstance(user, dict) else None
        if not isinstance(connection, dict):
            raise StatisticsError("GitHub response is missing the user's pull requests.")
        total = connection.get("totalCount")
        if type(total) is not int or total < 0:
            raise StatisticsError("GitHub returned an invalid pull request total.")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise StatisticsError("Pull request total changed during pagination; retry later.")
        nodes = connection.get("nodes")
        page_info = connection.get("pageInfo")
        if not isinstance(nodes, list) or len(nodes) > 100 or not isinstance(page_info, dict):
            raise StatisticsError("GitHub returned an invalid pull request page.")
        has_next = page_info.get("hasNextPage")
        if not isinstance(has_next, bool):
            raise StatisticsError("GitHub returned invalid pagination metadata.")

        previous_count = len(seen_prs)
        for node in nodes:
            pr_id, repo_id, eligible = parse_pull_request(node, login)
            if pr_id in seen_prs:
                continue
            seen_prs.add(pr_id)
            if eligible:
                merged_prs.add(pr_id)
                repositories.add(repo_id)
        if len(seen_prs) > expected_total:
            raise StatisticsError("Pull request nodes exceed the reported total.")
        if not has_next:
            if len(seen_prs) != expected_total:
                raise StatisticsError("GitHub returned an incomplete pull request connection.")
            return ContributionStats(len(merged_prs), len(repositories))

        next_cursor = require_string(page_info.get("endCursor"), "pagination cursor")
        if next_cursor in cursors or next_cursor == after:
            raise StatisticsError("GitHub pagination cursor did not advance.")
        if len(seen_prs) == previous_count or len(seen_prs) == expected_total:
            raise StatisticsError("GitHub pagination made no progress toward the reported total.")
        cursors.add(next_cursor)
        after = next_cursor


def render_svg(stats, snapshot_date, login, *, dark=False):
    accent, text = ("#58A6FF", "#8B949E") if dark else ("#0969DA", "#57606A")
    login = html.escape(login)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="360" height="170" viewBox="0 0 360 170" role="img" aria-labelledby="title description">
  <title id="title">External contributions</title>
  <desc id="description">{stats.merged_prs} merged pull requests across {stats.repositories} external public repositories, authored by {login} and excluding repositories owned by {login}. All-time snapshot dated {snapshot_date} (UTC).</desc>
  <g font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif">
    <g fill="{accent}" font-size="44" font-weight="600">
      <text x="18" y="76">{stats.merged_prs}</text>
      <text x="194" y="76">{stats.repositories}</text>
    </g>
    <g fill="{text}" font-size="12">
      <text x="18" y="103">External merged PRs</text>
      <text x="194" y="103">External repositories</text>
    </g>
    <text x="18" y="146" fill="{text}" font-size="11">All-time external contributions · Snapshot: {snapshot_date}</text>
  </g>
</svg>
'''


def render_readme(content, stats, snapshot_date, login):
    image_url = (
        f"https://raw.githubusercontent.com/{login}/{login}/main/"
        "assets/external-contributions-light.svg"
    )
    pattern = re.compile(
        r'(<img src="' + re.escape(image_url) + r'" alt=")[^"]*(" width="360" />)',
        re.IGNORECASE,
    )
    alt = (
        f"All-time external contributions: {stats.merged_prs} merged PRs across "
        f"{stats.repositories} external public repositories. Snapshot: {snapshot_date} (UTC). "
        f"Excludes repositories owned by {login}."
    )
    updated, count = pattern.subn(lambda match: match[1] + alt + match[2], content)
    if count != 1:
        raise StatisticsError("Expected exactly one external contributions image in README.md.")
    return updated


def write_outputs(outputs):
    """Stage every changed output before replacing files, leaving identical files untouched."""
    staged = []
    try:
        for path, content in outputs.items():
            if path.exists() and path.read_text(encoding="utf-8") == content:
                continue
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=path.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                staged.append((temporary_path, path))
                temporary.write(content)
        for temporary_path, path in staged:
            temporary_path.replace(path)
    finally:
        for temporary_path, _ in staged:
            temporary_path.unlink(missing_ok=True)


def update_profile(root, login, token, *, snapshot_date=None, query_runner=None):
    readme_path = root / "README.md"
    content = readme_path.read_text(encoding="utf-8")
    stats = fetch_statistics(login, token, query_runner=query_runner)
    snapshot_date = snapshot_date or datetime.now(timezone.utc).date()
    outputs = {
        readme_path: render_readme(content, stats, snapshot_date, login),
        root / "assets/external-contributions-light.svg": render_svg(stats, snapshot_date, login),
        root / "assets/external-contributions-dark.svg": render_svg(stats, snapshot_date, login, dark=True),
    }
    write_outputs(outputs)
    return stats


def main():
    try:
        stats = update_profile(
            Path(__file__).resolve().parents[1],
            os.environ.get("GITHUB_LOGIN", "Boulea7"),
            os.environ.get("GITHUB_TOKEN", ""),
        )
    except (StatisticsError, OSError) as error:
        print(f"External contribution update failed: {error}", file=sys.stderr)
        return 1
    print(f"External contributions: {stats.merged_prs} merged PRs across {stats.repositories} repositories.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
