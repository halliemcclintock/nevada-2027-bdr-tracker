# Nevada 2027 BDR Tracker

A static, responsive legislative watchlist for Burke Construction Group, based on the November 7, 2026 senior staff memo.

## Open the dashboard

After configuring GitHub Pages, the expected URL is:

https://halliemcclintock.github.io/nevada-2027-bdr-tracker/

The site loads data/bdrs.json, data/watchlist.json, and data/meta.json in the browser. No JavaScript build step, framework or external fonts are required.

To preview locally, run `python -m http.server 8000` in the repository root and open http://localhost:8000. Opening index.html directly as a file will not load its JSON data.

## Configure automated updates

1. Obtain a LegiScan API key from https://legiscan.com/legiscan.
2. In the GitHub repository, open Settings > Secrets and variables > Actions.
3. Choose New repository secret, enter `LEGISCAN_API_KEY` as the name, paste your key as its value, and save.
4. Under Settings > Pages, set the source to **GitHub Actions**. GitHub Pages on a private repository depends on the account's Pages eligibility; if it is unsupported, make the repository public only after reviewing the legislative memo data and whether the material is intended to be publicly accessible.
5. Under Actions, choose **Weekly Nevada BDR update and Pages deployment**, then **Run workflow** on `main`.

The update runs every Monday at **14:00 UTC** and can also be started manually. A successful update commits only changed files under `data/`. Pushing code or data to `main` also redeploys the site without invoking the paid APIs.

## Run locally

Install Python 3.12+ and then run:

```sh
python -m pip install -r requirements.txt
export LEGISCAN_API_KEY="your-key"
python scripts/update.py --dry-run
python scripts/update.py
```

In Windows PowerShell, set the key with `$env:LEGISCAN_API_KEY="your-key"` rather than `export`.

Never add API keys to the repository files.

## Edit the watchlist

Edit `data/watchlist.json`. Each row has `term`, `tier` (exactly `Core construction` or `Business-wide`), and `patterns` (semicolon-separated phrases).

A phrase matches case-insensitively with a word boundary at the beginning but not at the end. Thus `abatement` matches `abatements`. Only title, summary, reference and watch_for are checked. Any construction match wins over business-wide matches. The flag totals count issues, not terms.

New items are those whose `date_added` equals the latest date in `data/bdrs.json`. This is intentionally distinct from the last successful refresh date in `data/meta.json`.

## Automated updater behavior

`scripts/update.py` reads NELIS BDR ranges from `HomeBdr/GetBdrsForNavSubPanel`, using a descriptive User-Agent and a one-second delay between requests. It gets the 2027 regular session's LegiScan master list, fetches new or changed bill details, and maintains an optional `data/legiscan_cache.json`.

The updater **never deletes items and never overwrites rank, title or watch_for**. It fills summaries only when blank, adds watchlist-matching BDRs or bills, updates bill references and stages, and logs changes in `data/changelog.md`. Source errors cause a visible failed workflow and do not silently replace existing data.

**Source verification needed:** The NELIS endpoint was identified from the supplied reference URLs, but live network access to its actual response was unavailable during development. The parser accepts plausible HTML table and JSON response formats, and fails if it cannot parse any rows. Inspect the first manual workflow's logs before relying on its scheduled operation. The endpoint's response schema and any bill overview ID mapping may need adjustment. LegiScan bill IDs are not NELIS overview IDs, so the importer does not invent NELIS overview links.

The original 20 memo priorities and their 2026 analysis are preserved, not automatically editorially rewritten. Review any outdated political or legislative statements separately.

## Troubleshooting

If Pages deployment fails, check private-repository Pages eligibility and the Pages setting. If update fails, inspect the Actions logs for NELIS parsing, the LegiScan API secret, API quota, or the availability of the Nevada 2027 session. If the dashboard looks blank locally, use an HTTP server rather than opening the file directly.
