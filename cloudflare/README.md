# Cloudflare R2 storage

Structured bot state is encrypted and stored in Cloudflare D1; images and other media are stored in a private Cloudflare R2 bucket. GUI and Railway use the same `BOT_CONTROL_ID`, so both read and write the same R2 data.

## Local / GUI setup

```powershell
cd cloudflare
npm install
npm run r2:create
cd ..
python -m pip install -r requirements.txt
```

Set these values in the project `.env`:

```env
BOT_ENCRYPTION_KEY=your_same_fernet_key
BOT_CONTROL_ID=production
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=<R2 access key id>
R2_SECRET_ACCESS_KEY=<R2 secret access key>
R2_BUCKET_NAME=meta-bot-media
DISCORD_BOT_TOKEN=optional_if_saved_from_GUI
ADMIN_USER_ID=<discord admin id>
ALLOWED_GUILD_ID=1517192928068636703
```

## Railway setup

Railway Variables-e exactly ei values add koro:

- `BOT_ENCRYPTION_KEY`: local `.env`-er same key
- `BOT_CONTROL_ID`: local-er same value, usually `production`
- `R2_ENDPOINT`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME`
- `ADMIN_USER_ID`
- `ALLOWED_GUILD_ID`
- `DISCORD_BOT_TOKEN`: optional; na dile Railway R2 theke encrypted token load korbe

Railway automatically `Dockerfile` use kore `python railway_bot.py` run korbe. GUI diye settings save korar por Railway normally next polling cycle-e, maximum about 10 seconds-er moddhe update pabe.

Same Discord token diye GUI ebong Railway ekshathe connected rekho na. GUI configuration-er jonno use koro; Railway-ke 24/7 worker hisebe online rakho.

