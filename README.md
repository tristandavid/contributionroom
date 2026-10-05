# Contribution Room website

A plain static site (no build step) for GitHub Pages: landing page, privacy policy, terms, and the public `limits.json` the apps read.

## Publish on GitHub Pages
1. Create a repository (for example `contribution-room-site`) and push these files to the `main` branch root.
2. Repository **Settings → Pages → Build and deployment → Source: Deploy from a branch**, branch `main`, folder `/ (root)`.
3. The site appears at `https://<your-user>.github.io/<repo>/`.

### Your domain
The `CNAME` file is already set to `contributionroom.wealthboardapp.com`. In your DNS, add a `CNAME` record for `contributionroom` pointing to `<your-github-user>.github.io`, then tick **Enforce HTTPS** in Settings → Pages once it is offered. GitHub Pages serves `privacy.html` at `/privacy` and `terms.html` at `/terms`, which are the addresses the apps use.

### Using a different domain later
1. In **Settings → Pages → Custom domain**, enter your domain (this creates a `CNAME` file).
2. At your DNS provider, point the domain at GitHub Pages (a `CNAME` record to `<your-user>.github.io` for a subdomain, or the four GitHub Pages `A` records for a root domain). Tick **Enforce HTTPS** when it is offered.

## Before you launch
- Search for `[DATE]`, `[YOUR NAME OR COMPANY]` and `[YOUR PROVINCE OR STATE AND COUNTRY]` (they are highlighted on the pages) and replace them.
- Replace the two disabled store buttons in `index.html` with your Google Play and App Store links.
- Have the privacy policy and terms checked by someone qualified. They describe what the app does today (on-device data, no analytics, store billing). If you add analytics, ads or a server, update them first.

## Connect the apps
- **Already wired:** the apps use `https://contributionroom.wealthboardapp.com/privacy`, `/terms` and `/limits.json`.
- **Limits updates:** `limits.json` here is what the apps download (once a day). A GitHub Action adds each new year automatically, see below. You can still edit it by hand: bump `updated`, commit, keep it valid JSON with `"schema": 1`.

## Automatic yearly limits

`.github/workflows/update-limits.yml` runs every day. From 1 September it checks the official pages for next year's limits:
- **Canada (TFSA, RRSP):** read from the CRA's limits table.
- **US (IRA, 401(k), SIMPLE, SEP, HSA, FSA, ...):** short snippets of IRS pages are read by GitHub Models (free, uses the built-in token). Every number must appear word for word in those snippets and be a plausible step from last year.
- **Fixed by law** (FHSA, RESP, RDSP, Coverdell, catch-up ages) are copied forward. 457(b), Solo 401(k) and ABLE follow the 401(k), 415(c) and gift-exclusion numbers.
- Nothing is published until every required number is found and checked. HSA, FSA, dependent care and the gift exclusion are copied forward if their page is not updated yet, listed under `"carried"` in `limits.json`, retried daily, and an issue is opened so you know.
- If something looks wrong, the run fails and GitHub emails you. If a year is still missing after 15 January, it fails too.

Try it by hand: **Actions > Update contribution limits > Run workflow** (leave "Check only" ticked). Run the tests with `python3 scripts/test_update_limits.py`.
If an IRS page moves, edit `IRS_PAGES` at the top of `scripts/update_limits.py`.
Make sure **Settings > Actions > General** allows workflows, and that your GitHub notification settings send failed workflow emails.
