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
- Replace `[YOUR PROVINCE OR STATE AND COUNTRY]` in terms.html (highlighted on the page). The date and company name are filled in.
- Replace the two disabled store buttons in `index.html` with your Google Play and App Store links.
- Have the privacy policy and terms checked by someone qualified. They describe what the app does today (on-device data, no analytics, store billing). If you add analytics, ads or a server, update them first.

## Connect the apps
- **Already wired:** the apps use `https://contributionroom.wealthboardapp.com/privacy`, `/terms` and `/limits.json`.
- **Automatic limits updates:** `limits.json` here is a copy of the one bundled in the apps. When a limit changes, edit `limits.json`, bump its `updated` date, commit, and every installed app picks it up. Keep it valid JSON with `"schema": 1`.
