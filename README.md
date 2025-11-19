# TLV Rentals Bot (daily email digest)

A tiny scraper that watches your saved search URLs (Yad2, Homeless, etc.), detects new listings, and emails you a daily digest.

## Quickstart

1. **Download** this folder and push to a new GitHub repo.
2. Edit `config.yaml`:
   - Put your Gmail in `from_email` and your recipient(s) in `to_emails`.
   - Replace the example `sources` with your own search URLs (with your filters).
   - Adjust `filters` (min rooms, size, price, keywords).
3. In your Google Account, enable 2‑Step Verification and create an **App Password** for "Mail".
4. In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**.
   - Name: `GMAIL_APP_PASSWORD`
   - Value: the app password from step 3.
5. Go to **Actions** tab → select the workflow → **Run workflow** to test.
6. The workflow is scheduled daily via cron (see `.github/workflows/daily.yml`).

> Notes
- This bot is intentionally simple/robust: it pulls links from your search pages and deduplicates via SQLite. If a site changes markup, you still get new links.
- Facebook groups are not supported (fragile & against ToS). Prefer site email alerts + add their inbox to your daily digest in a future Gmail-API version.
- Respect each site's Terms of Service.

## How “new listings only” works

- Every run updates `seen_listings.sqlite3` with each listing URL it emails.
- The GitHub Action restores that SQLite file from cache before running and saves it afterward, so each day only unseen links are sent.
- If you ever want to reset the history (and receive every listing again), delete `seen_listings.sqlite3`, commit the removal, and push; the next run will start a fresh cache.

## Local test

```bash
pip install -r requirements.txt
export GMAIL_APP_PASSWORD=your-app-password
python main.py
```

## Customization ideas
- Add Telegram/Slack notifications.
- Per-site parsers to extract price/rooms/size more reliably.
- Highlight differences day-over-day.
