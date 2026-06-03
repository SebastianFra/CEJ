# CEJ apartment watcher 🏠🔔

Watches the CEJ (bolig.io) listings page for **Sjælland** and sends an instant
push notification to your phone every time a new apartment appears.

- **Watched page:** <https://udlejning.cej.dk/find-bolig/overblik?p=sjælland>
- **Notifications:** [ntfy.sh](https://ntfy.sh) push (free, no account needed)
- **Runs on:** GitHub Actions, every 5 minutes — no computer needs to stay on.

---

## 1. Get notifications on your phone (2 minutes)

1. Install the **ntfy** app:
   - iPhone: App Store → "ntfy"
   - Android: Play Store / F-Droid → "ntfy"
2. Open the app → **Subscribe to topic** → enter exactly:

   ```
   cej-sjaelland-62ce0459ece5
   ```

3. Done. Every new CEJ apartment on Sjælland will buzz your phone, and tapping
   the notification opens the listing.

> The topic name is your private "channel". Anyone who knows it can read these
> notifications, so don't share it publicly. To rotate it, change `ntfy_topic`
> in `config.json` (and re-subscribe in the app).

## 2. Turn on the watcher (GitHub Actions)

1. Push this repo to GitHub (the assistant has already done this on the
   `claude/kind-meitner-IfSnh` branch — merge it to your default branch).
2. On GitHub: **Settings → Actions → General → Workflow permissions** →
   select **Read and write permissions** → Save.
   (This lets the workflow remember which apartments it already alerted you to.)
3. Go to the **Actions** tab → enable workflows if prompted →
   open **"Watch CEJ apartments"** → **Run workflow** to do the first run now.

That's it. It will keep running every 5 minutes on its own.

## How it works

- A headless Chrome (Playwright) opens the page like a real browser — required
  because the site blocks plain scripts/curl with a `403`.
- It captures the JSON the page loads to render apartments, and falls back to
  reading the page DOM if needed.
- New apartments are diffed against `state/seen.json` (committed back each run),
  so you only get alerted **once** per apartment.
- The **first run** records everything currently listed as a baseline and does
  **not** notify (so you aren't hit with dozens of alerts at once). From then on
  you only get genuinely new ones. To be notified on the first run too, set
  `notify_on_first_run` to `true` in `config.json`.

## Important limitations (please read)

1. **"Immediate" has a floor of ~5 min.** GitHub Actions' fastest schedule is
   every 5 minutes, and scheduled runs are frequently delayed several more
   minutes when GitHub is busy. So expect alerts within roughly 5–15 minutes of
   a listing appearing — not literally instant. If you need true sub-minute
   speed, this needs to run on an always-on server instead (cron every 60s).
2. **Bot protection.** The site sits behind Cloudflare-style protection. The
   real browser approach is designed to get past it, but if GitHub's runner IPs
   get challenged, runs may extract 0 listings. Each run uploads a `debug/`
   artifact (rendered HTML + captured JSON) so the selectors/approach can be
   tuned. **Check the first run's logs and artifact to confirm it found
   listings.** This could not be tested ahead of time because the build sandbox
   itself is blocked from reaching the site.

## Configuration

Edit `config.json`:

| Key | Meaning |
| --- | --- |
| `url` | The listings page to watch |
| `ntfy_topic` | Your private notification channel |
| `ntfy_server` | ntfy server (default `https://ntfy.sh`) |
| `notify_on_first_run` | Alert on every existing listing the first time (default `false`) |
| `max_notifications_per_run` | Safety cap to avoid alert floods |

You can override the topic/server with repo **secrets** `NTFY_TOPIC` /
`NTFY_SERVER` instead of committing them.

## Run it locally (optional)

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium
python watcher.py
```
