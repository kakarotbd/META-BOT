# MetaBot Cloudflare and Railway setup

MetaBot stores shared settings and bot records in encrypted Cloudflare storage. The desktop GUI and Railway worker must use the same D1 database, R2 bucket, `BOT_CONTROL_ID`, and `BOT_ENCRYPTION_KEY`.

## Local GUI

From the project root:

```powershell
Copy-Item .env.example .env
python -m pip install -r requirements.txt
python bot.py
```

Add your private Discord and Cloudflare values to `.env`. The template has placeholders only, and `.gitignore` excludes the real `.env` file. Set the displayed bot name and tagline in the root `bot_app_config.txt` file.

## Railway worker

Deploy this repository with the included Dockerfile and add the same values from `.env` as Railway service variables. It starts `python -m workers.railway`; no public port is required.

Keep one worker replica. The worker polls shared settings every 10 seconds. Use the desktop GUI to configure features, then disconnect it and leave Railway as the only active Discord gateway for that token.

## Ticket moderators

Ticket staff IDs are configured per server in the GUI's Tickets tab. They are not read from environment variables. A server with an empty saved staff list cannot create tickets; add and save at least one moderator in the GUI to enable ticket creation.