import io
import json
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
WORKFLOW_PATH = ROOT / ".github/workflows/update-readme-stats-year.yml"
WORKFLOW = WORKFLOW_PATH.read_text(encoding="utf-8")
ANNUAL_CODE = textwrap.dedent(
    WORKFLOW.split("          python3 - <<'PY'\n", 1)[1].split("          PY\n", 1)[0]
)


def annual_page(*, commits=("upstream/commit",), commit_total=None,
                issues=(), issue_next=None, prs=(), pr_next=None,
                reviews=(), created=()):
    def connection(names, nested_key, cursor=None):
        nodes = [{"repository": {"nameWithOwner": name}} for name in names]
        if nested_key:
            nodes = [{nested_key: node} for node in nodes]
        return {"nodes": nodes,
                "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}

    return {"data": {"user": {"contributionsCollection": {
        "totalRepositoriesWithContributedCommits": (
            len(commits) if commit_total is None else commit_total),
        "commitContributionsByRepository": [
            {"repository": {"nameWithOwner": name}} for name in commits],
        "issueContributions": connection(issues, "issue", issue_next),
        "pullRequestContributions": connection(prs, "pullRequest", pr_next),
        "pullRequestReviewContributions": connection(reviews, "pullRequest"),
        "repositoryContributions": connection(created, None),
    }}}}


class AnnualWorkflowTests(unittest.TestCase):
    def run_annual(self, pages):
        self.writer = Mock()
        self.urlopen = Mock(side_effect=[io.BytesIO(json.dumps(page).encode())
                                         for page in pages])
        with patch.dict("os.environ", {"CURRENT_YEAR": "2026", "GITHUB_LOGIN": "Boulea7",
                                       "GITHUB_TOKEN": "test-token"}), \
                patch.object(Path, "read_text", return_value=README), \
                patch.object(Path, "write_text", self.writer), \
                patch("urllib.request.urlopen", self.urlopen):
            exec(compile(ANNUAL_CODE, str(WORKFLOW_PATH), "exec"), {})
        return self.writer.call_args.args[0] if self.writer.called else README

    def test_connections_finish_independently_without_processing_restarted_pages(self):
        updated = self.run_annual([
            annual_page(issues=("upstream/issue-1",), issue_next="issue-2",
                        prs=("upstream/pr-1",), pr_next="pr-2",
                        reviews=("upstream/review",), created=("upstream/created",)),
            annual_page(issues=("upstream/issue-2",), prs=("upstream/pr-2",),
                        pr_next="pr-3", reviews=("upstream/ignored-review",)),
            annual_page(issues=("upstream/ignored-issue",), prs=("upstream/pr-3",),
                        created=("upstream/ignored-created",)),
        ])
        self.assertIn("Contributed to 8 repositories in 2026", updated)
        variables = [json.loads(call.args[0].data)["variables"]
                     for call in self.urlopen.call_args_list]
        self.assertEqual([value["issueAfter"] for value in variables],
                         [None, "issue-2", "issue-2"])
        self.assertEqual([value["prAfter"] for value in variables],
                         [None, "pr-2", "pr-3"])
        self.assertTrue(all(call.kwargs["timeout"] == 30
                            for call in self.urlopen.call_args_list))

    def test_truncated_commit_repository_list_preserves_previous_stats(self):
        with self.assertRaisesRegex(SystemExit, "incomplete"):
            self.run_annual([annual_page(commits=tuple(f"upstream/repo-{i}"
                                                      for i in range(100)),
                                        commit_total=101)])
        self.writer.assert_not_called()

    def test_repeated_cursor_preserves_previous_stats(self):
        with self.assertRaisesRegex(SystemExit, "cursor did not advance"):
            self.run_annual([annual_page(issue_next="repeat"),
                             annual_page(issue_next="repeat")])
        self.writer.assert_not_called()

    def test_iteration_limit_preserves_previous_stats(self):
        with self.assertRaisesRegex(SystemExit, "safety limit"):
            self.run_annual([annual_page(issue_next=f"cursor-{number}")
                             for number in range(1000)])
        self.assertEqual(self.urlopen.call_count, 1000)
        self.writer.assert_not_called()

    def test_zero_annual_contributions_is_valid(self):
        updated = self.run_annual([annual_page(commits=())])
        self.assertIn("Contributed to 0 repositories in 2026", updated)


if __name__ == "__main__":
    unittest.main()
