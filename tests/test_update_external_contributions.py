import io
import json
import re
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import update_external_contributions as stats


LOGIN = "Boulea7"
ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def pull_request(number, *, repository=None, owner="upstream", author=LOGIN,
                 private=False, state="MERGED"):
    repository = repository or f"R{number}"
    return {
        "id": f"P{number}", "state": state,
        "author": {"login": author} if author is not None else None,
        "repository": {
            "id": repository, "nameWithOwner": f"{owner}/{repository}",
            "isPrivate": private, "owner": {"login": owner},
        },
    }


def page(nodes, *, total=None, next_cursor=None):
    return {"data": {"user": {"pullRequests": {
        "nodes": nodes, "totalCount": len(nodes) if total is None else total,
        "pageInfo": {"hasNextPage": next_cursor is not None,
                     "endCursor": next_cursor},
    }}}}


class PaginationTests(unittest.TestCase):
    def fetch(self, *pages):
        runner = Mock(side_effect=pages)
        result = stats.fetch_statistics(LOGIN, "test-token", query_runner=runner)
        return result, runner

    def test_multiple_pages_deduplicate_prs_and_repositories(self):
        first = pull_request(1, repository="shared")
        second = pull_request(2, repository="shared")
        result, runner = self.fetch(
            page([first, second], total=3, next_cursor="page-2"),
            page([second, pull_request(3)], total=3),
        )
        self.assertEqual(result, stats.ContributionStats(3, 2))
        self.assertEqual([call.args[0]["after"] for call in runner.call_args_list],
                         [None, "page-2"])

    def test_only_authored_public_external_merged_prs_count(self):
        result, _ = self.fetch(page([
            pull_request(1, author="boulea7"),
            pull_request(2, owner="bOuLeA7"),
            pull_request(3, private=True),
            pull_request(4, author="another-user"),
            pull_request(5, state="CLOSED"),
            pull_request(6, author=None),
        ]))
        self.assertEqual(result, stats.ContributionStats(1, 1))

    def test_more_than_search_limit_is_fully_paginated(self):
        nodes = [pull_request(number) for number in range(1105)]
        pages = [page(nodes[start:start + 100], total=len(nodes),
                      next_cursor=f"page-{start + 100}"
                      if start + 100 < len(nodes) else None)
                 for start in range(0, len(nodes), 100)]
        result, runner = self.fetch(*pages)
        self.assertEqual(result, stats.ContributionStats(1105, 1105))
        self.assertEqual(runner.call_count, 12)
        self.assertIn("pullRequests(states: MERGED, first: 100, after: $after)",
                      stats.QUERY)

    def test_real_zero_is_valid(self):
        result, _ = self.fetch(page([]))
        self.assertEqual(result, stats.ContributionStats(0, 0))

    def test_changed_total_count_fails(self):
        with self.assertRaises(stats.StatisticsError):
            self.fetch(page([pull_request(1)], total=2, next_cursor="next"),
                       page([pull_request(2)], total=3))


class RequestTests(unittest.TestCase):
    @patch.object(stats.time, "sleep")
    @patch.object(stats.urllib.request, "urlopen")
    def test_transient_network_failure_retries_with_timeout(self, urlopen, sleep):
        payload = page([])
        urlopen.side_effect = [urllib.error.URLError("temporary"),
                              io.BytesIO(json.dumps(payload).encode())]
        self.assertEqual(stats.run_query("test-token", {"login": LOGIN}), payload)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], stats.REQUEST_TIMEOUT)
        sleep.assert_called_once_with(1)

    @patch.object(stats.time, "sleep")
    @patch.object(stats.urllib.request, "urlopen")
    def test_server_failure_retries(self, urlopen, sleep):
        urlopen.side_effect = [
            urllib.error.HTTPError("https://api.github.com/graphql", 503,
                                   "private response", {}, None),
            io.BytesIO(json.dumps(page([])).encode()),
        ]
        self.assertEqual(stats.run_query("test-token", {}), page([]))
        self.assertEqual(urlopen.call_count, 2)

    @patch.object(stats.time, "sleep")
    @patch.object(stats.urllib.request, "urlopen")
    def test_permanent_http_failure_is_not_retried_or_leaked(self, urlopen, sleep):
        urlopen.side_effect = urllib.error.HTTPError(
            "https://api.github.com/graphql", 401, "secret-token", {}, None)
        with self.assertRaises(stats.StatisticsError) as error:
            stats.run_query("secret-token", {})
        self.assertNotIn("secret-token", str(error.exception))
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    @patch.object(stats.urllib.request, "urlopen")
    def test_invalid_json_fails(self, urlopen):
        urlopen.return_value = io.BytesIO(b"invalid private response")
        with self.assertRaises(stats.StatisticsError) as error:
            stats.run_query("test-token", {})
        self.assertNotIn("private response", str(error.exception))


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "assets").mkdir()
        (self.root / "README.md").write_text(README, encoding="utf-8")
        for theme in ("light", "dark"):
            (self.root / f"assets/external-contributions-{theme}.svg").write_text(
                f"old {theme} svg", encoding="utf-8")

    def files(self):
        return {path.relative_to(self.root): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def update(self, *pages):
        return stats.update_profile(self.root, LOGIN, "test-token",
                                    snapshot_date=date(2026, 10, 10),
                                    query_runner=Mock(side_effect=pages))

    def test_readme_and_both_themes_are_synchronized(self):
        self.update(page([pull_request(1), pull_request(2)]))
        updated = (self.root / "README.md").read_text(encoding="utf-8")
        expected = re.sub(
            r'alt="All-time external contributions:[^"]*"',
            'alt="All-time external contributions: 2 merged PRs across 2 external '
            "public repositories. Snapshot: 2026-10-10 (UTC). "
            'Excludes repositories owned by Boulea7."',
            README,
        )
        self.assertEqual(updated, expected)
        self.assertIn('width="100%" alt="GitHub contribution snake animation"', updated)
        for theme, accent, text in (("light", "#0969DA", "#57606A"),
                                    ("dark", "#58A6FF", "#8B949E")):
            svg = (self.root / f"assets/external-contributions-{theme}.svg").read_text()
            self.assertIn('width="360" height="170"', svg)
            self.assertIn(f'fill="{accent}"', svg)
            self.assertIn(f'fill="{text}"', svg)
            self.assertIn('x="18" y="76">2</text>', svg)
            self.assertIn('x="194" y="76">2</text>', svg)
            self.assertIn("2026-10-10 (UTC)", svg)
            self.assertIn("public repositories", svg)

    def test_previously_refreshed_readme_can_be_updated_again(self):
        self.update(page([pull_request(1)]))
        first = (self.root / "README.md").read_text(encoding="utf-8")
        self.update(page([pull_request(1), pull_request(2)]))
        second = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertEqual(second, first.replace("1 merged PRs across 1 external",
                                               "2 merged PRs across 2 external"))

    def test_same_data_and_date_is_idempotent(self):
        self.update(page([]))
        before = self.files()
        mtimes = {path: path.stat().st_mtime_ns for path in self.root.rglob("*.svg")}
        self.update(page([]))
        self.assertEqual(self.files(), before)
        self.assertEqual({path: path.stat().st_mtime_ns for path in mtimes}, mtimes)

    def test_api_and_pagination_failures_preserve_all_files(self):
        missing_repository = pull_request(1)
        missing_repository["repository"] = None
        malformed_repository = pull_request(1)
        malformed_repository["repository"]["isPrivate"] = None
        empty_with_next = page([], total=1, next_cursor="next")
        failures = [
            [{"errors": [{"message": "private API response"}]}],
            [{"data": {"user": None}}],
            [page([missing_repository])],
            [page([malformed_repository])],
            [page([pull_request(1)], total=2)],
            [page([pull_request(1)], total=3, next_cursor="repeat"),
             page([pull_request(2)], total=3, next_cursor="repeat")],
            [empty_with_next],
        ]
        for responses in failures:
            with self.subTest(responses=responses):
                before = self.files()
                with self.assertRaises(stats.StatisticsError):
                    self.update(*responses)
                self.assertEqual(self.files(), before)

    @patch.object(stats.time, "sleep")
    @patch.object(stats.urllib.request, "urlopen")
    def test_exhausted_network_retries_preserve_all_files(self, urlopen, sleep):
        urlopen.side_effect = urllib.error.URLError("secret-token")
        before = self.files()
        with self.assertRaises(stats.StatisticsError) as error:
            stats.update_profile(self.root, LOGIN, "secret-token")
        self.assertNotIn("secret-token", str(error.exception))
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(self.files(), before)

    def test_missing_readme_anchor_preserves_all_files(self):
        (self.root / "README.md").write_text("Unrelated README\n", encoding="utf-8")
        before = self.files()
        with self.assertRaises(stats.StatisticsError):
            self.update(page([]))
        self.assertEqual(self.files(), before)


if __name__ == "__main__":
    unittest.main()
