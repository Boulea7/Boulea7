# Profile statistics

The **Update Profile Stats** workflow refreshes the profile every six hours at
00:17, 06:17, 12:17, and 18:17 UTC. In China Standard Time, the daily run times are
02:17, 08:17, 14:17, and 20:17. GitHub Actions may delay scheduled runs. GitHub
disables scheduled workflows in public repositories after 60 days without
repository activity.

To refresh manually, open **Actions → Update Profile Stats → Run workflow** on
the `main` branch. Changes to the README, updater, tests, or workflow on `main`
also trigger a refresh. The workflow uses the automatic `GITHUB_TOKEN` with
repository contents write permission; no personal access token is needed.

The external contribution cards count all-time merged pull requests authored by
Boulea7 in public repositories whose current owner is someone else. External
forks are included. Pull requests and repositories are deduplicated by their
GitHub IDs. Author and owner login comparisons ignore case. The updater walks
the entire GraphQL pull request connection, without the Search API's 1000-result
limit. The snapshot date is UTC. The README alt text and both color themes use
the same result.

The annual **Contributed to** badge counts distinct repositories with commits,
issues, pull requests, reviews, or repository creation visible through GitHub's
contributions API in the current UTC year. It has a different scope from the
public external merged PR cards. If GitHub's unpaginated commit repository list
is truncated, the workflow fails instead of publishing a partial annual count.

API failures, incomplete pagination, and missing README anchors stop the update.
The workflow commits the README and both SVG cards together only after both
updaters succeed. A failed run leaves the previously published statistics in
place; inspect the failed Actions step and run the workflow again after resolving
the error. Identical results and snapshot dates produce no new commit.

Run the local checks with Python's standard library:

```sh
python3 -B -m unittest discover -s tests -v
```
