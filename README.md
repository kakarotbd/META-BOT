# MetaBot

MetaBot is a Discord community bot with a Windows desktop control panel. Configure it in the panel and keep it online with the included Railway worker. Both use the same Cloudflare database and file storage.

> **Security:** An earlier `.env.example` contained live-looking credentials. If you used or published those values, revoke or rotate the Discord and Cloudflare credentials before deployment. The template now contains placeholders only.

## Features

- **Chat and announcements:** send a message through a form, schedule role notifications, post movie announcements, and create custom GIF slash commands.
- **Community:** welcome and goodbye messages, birthday collection and wishes, counting channels, automatic roles, text and voice XP, and an XP leaderboard.
- **Moderation:** link and blocked-word rules, warnings, timeouts, kicks, and bans. The configured administrator is protected from the bot's kick and ban commands.
- **Bot anti-spam:** checks visible server messages from bots for mass mentions, rapid bursts, and repeated messages. It tries to ban the suspected bot and sends an alert to the configured administrator. It cannot see private DMs.
- **Tickets:** ticket panels, private channels, staff assignment and transfer, notes, closing, member ratings, and staff rating summaries.
- **Voice:** temporary rooms, room controls, member moves, and drag permissions.
- **Control panel:** settings, connection status, live logs, database usage, and JSON backup export/import.

The desktop panel has tabs for Overview, GIF Commands, Chat, Link Send, Drag Permissions, Tickets, Voice, Greetings, Leveling, Counting, Birthdays, Notifications, Movies, Auto Roles, Moderation, Word Block, and Live Logs. Use **All Controls** to jump to a tab.

## Discord commands

Use these commands in the configured server:

| Command | Description |
| --- | --- |
| `/help` | Browse the feature guide. |
| `/chat` | Open a form to post in the current channel. |
| `/leaderboard` | Show the top 10 members by total XP. |
| `/move` | Select members and move them to a voice channel. |
| Custom GIF slash commands | Run commands created in the GIF Commands tab. |
| DM `start` | A configured ticket staff member can request a support rating summary. |

The code sets `!` as a message-command prefix, but it does not register user-facing `!` commands. Use the slash commands above.

## Requirements

- Windows for the desktop control panel.
- Python 3.12.
- A Discord application and bot token.
- A Cloudflare account with a D1 database and a private R2 bucket.
- Node.js and npm only if you want to create Cloudflare resources with Wrangler.
- A Railway account only if you want to host the always-on worker.

## 1. Create and invite a Discord bot

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Copy the bot token and keep it private.
3. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent** and **Server Members Intent**.
4. Enable Developer Mode in Discord, then copy your user ID and the ID of the one server this bot will serve.
5. Invite the bot to that server. The built-in invite requests Administrator permissions because temporary voice rooms and moderation need broad access. If you make a custom invite, grant the needed permissions and place the bot's role high enough to manage the roles and members you select.

This project is configured for one server ID through `ALLOWED_GUILD_ID`.

## 2. Set up Cloudflare storage

The bot needs two things in Cloudflare: a **D1 database** for its records and an **R2 bucket** for files. Do these steps in the Cloudflare dashboard. You do not need Node.js or Wrangler for this method.

### Make the D1 database

1. Sign in to [Cloudflare](https://dash.cloudflare.com/).
2. Open **Storage & databases → D1 SQL Database** (the menu may simply say **D1**).
3. Select **Create database**.
4. Name it `meta-bot-db` and create it.
5. Open the new database and copy its **Database ID**. You will put this in `.env` as `D1_DATABASE_ID`.

You do not need to create a table or paste SQL. MetaBot makes the tables it needs when it starts.

### Make the R2 bucket and its keys

1. Open **Storage & databases → R2 Object Storage**.
2. Select **Create bucket**, name it `meta-bot-media`, and create it. Keep the bucket private.
3. In R2, open **Manage R2 API tokens** (or **Manage** beside API Tokens), then create a token.
4. Choose **Object Read & Write**. Limit it to the `meta-bot-media` bucket if Cloudflare offers that option.
5. Create the token. Copy its **Access Key ID** and **Secret Access Key** now; Cloudflare will not show the secret again.
6. Copy your Cloudflare **Account ID** and the R2 S3 endpoint. The endpoint looks like `https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com`.

### Make a D1 API token

MetaBot also needs a Cloudflare API token to access D1:

1. Open your Cloudflare profile menu → **My Profile → API Tokens**.
2. Select **Create Token**, then choose a custom token.
3. Give it **Account → D1 → Edit** permission for the account that owns `meta-bot-db`.
4. Create the token and copy it. Put this value in `.env` as `D1_API_TOKEN`.

### Put the Cloudflare values in `.env`

Fill these entries with the values you just copied. Do not share the keys or commit `.env`.

```dotenv
CLOUDFLARE_ACCOUNT_ID=your_account_id
D1_DATABASE_ID=your_d1_database_id
D1_API_TOKEN=your_d1_api_token
R2_BUCKET_NAME=meta-bot-media
R2_ENDPOINT=https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=your_r2_access_key_id
R2_SECRET_ACCESS_KEY=your_r2_secret_access_key
```

Use the same account, database, bucket, and keys on Railway. If you create a new database, replace the `database_id` in `cloudflare/wrangler.toml` with its ID too if you plan to use the optional Wrangler commands. If you change either resource name, update its name in that file as well.

**Optional:** If you prefer the terminal, the `cloudflare` folder has Wrangler commands. Run `npm install` and `npx wrangler login` there, then `npm run d1:create` and `npm run r2:create`. The bot creates its D1 tables automatically, so `npm run d1:init` is not needed for a normal setup.

## 3. Configure and start the desktop bot

From the project root, create a virtual environment, install Python dependencies, and copy the environment template:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

If PowerShell blocks activation, run the environment's Python directly with `\.venv\Scripts\python.exe` instead.

Edit `.env` and fill in every value:

```dotenv
DISCORD_BOT_TOKEN=your_discord_bot_token
ADMIN_USER_ID=your_numeric_discord_user_id
ALLOWED_GUILD_ID=your_numeric_server_id
BOT_CONTROL_ID=production
BOT_ENCRYPTION_KEY=your_fernet_key
CLOUDFLARE_ACCOUNT_ID=your_cloudflare_account_id
D1_API_TOKEN=your_cloudflare_d1_api_token
D1_DATABASE_ID=your_d1_database_id
R2_ACCESS_KEY_ID=your_r2_access_key_id
R2_BUCKET_NAME=meta-bot-media
R2_ENDPOINT=https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com
R2_SECRET_ACCESS_KEY=your_r2_secret_access_key
```

Generate a Fernet key once, then copy its output to `BOT_ENCRYPTION_KEY`:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Use the same `BOT_CONTROL_ID`, `BOT_ENCRYPTION_KEY`, D1 database, R2 bucket, and Discord token on the desktop and Railway. Keep the encryption key safe: saved data and backups need it to be read.

Start the desktop control panel:

```powershell
python bot.py
```

Connect the bot, choose your server, and save settings in the tabs. Add at least one ticket staff member in **Tickets** before users can open tickets. Ticket staff are configured in the panel, not in `.env`.

The root `bot.py` starts the Windows desktop panel. `python -m workers.railway` starts the headless worker and is intended for Railway or another host.

## 4. Deploy the always-on worker to Railway

1. Push the project to a private GitHub repository.
2. Create a Railway project from that repository. Railway builds the included `Dockerfile`.
3. Add every variable from `.env` to the Railway service's **Variables** using the same values as your desktop setup.
4. Deploy with **one replica**. No public web port is needed.
5. After Railway connects, disconnect the bot in the desktop panel. Only one process should connect to Discord with this token at a time, or events may be handled twice.

Railway starts `python -m workers.railway`. The worker checks for saved settings about every 10 seconds and reconnects after temporary errors.

## Configuration

`bot_app_config.txt` sets the public bot name, tagline, and GUI logo path. It is safe to edit; do not put secrets in it.

| Variable | Required | Purpose |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | Yes | Discord bot login token. |
| `ADMIN_USER_ID` | Yes | Numeric user ID protected from bot kick/ban actions; receives anti-spam alerts. |
| `ALLOWED_GUILD_ID` | Yes | Numeric ID of the one server served by this bot. |
| `BOT_CONTROL_ID` | Recommended | Namespace shared by this installation, such as `production`. Defaults to `production`; use the same value on desktop and Railway. |
| `BOT_ENCRYPTION_KEY` | Yes | Fernet key used to encrypt stored data. Use the same key everywhere. |
| `CLOUDFLARE_ACCOUNT_ID` | Yes | Cloudflare account ID. |
| `D1_DATABASE_ID` | Yes | Cloudflare D1 database ID. |
| `D1_API_TOKEN` | Yes | Cloudflare API token with D1 access. |
| `R2_ENDPOINT` | Yes | Account's R2 S3 endpoint. |
| `R2_ACCESS_KEY_ID` | Yes | R2 S3 access key ID. |
| `R2_SECRET_ACCESS_KEY` | Yes | R2 S3 secret key. |
| `R2_BUCKET_NAME` | Yes | Private R2 bucket name. |

## Security

- Never share or commit `.env`, Discord tokens, Cloudflare API tokens, R2 keys, or Fernet keys. `.env` is ignored by Git and Docker. `.env.example` should contain placeholders only.
- Some copies of this project had live-looking credentials in `.env.example`. Treat any real values that were shared or published as exposed: revoke or rotate them in Discord and Cloudflare, then update your private `.env` and Railway variables. Editing a file does not revoke its old credentials.
- Do not change the Fernet key after saving data unless you have a migration plan. Backups contain server and member data; store them securely.
- Give the bot only the Discord permissions your enabled features need. Discord role hierarchy can prevent moderation and voice actions even when permissions are enabled.

## Project files

```text
bot.py                    Windows desktop control panel
bot_app_config.txt        Public display name, tagline, and logo path
config/                   Shared app configuration and .env loading
features/                 Chat, tickets, voice, birthday, anti-spam, and UI modules
runtime/                  Discord events, slash commands, and feature logic
storage/                  Cloudflare D1 and R2 data access
workers/                  Shared worker and Railway entry point
cloudflare/               Wrangler config, D1 schema, and storage commands
assets/                   Logo and other app assets
docs/                     Banner and documentation assets
requirements.txt          Python package list
Dockerfile                Railway worker image setup
```

## Troubleshooting

| Problem | Check |
| --- | --- |
| The app says a setting is missing | Make sure `.env` exists and all required values above are filled in. Restart the app after editing it. |
| `ADMIN_USER_ID` or `ALLOWED_GUILD_ID` is invalid | Use a numeric Discord user or server ID. Enable Developer Mode to copy IDs. |
| D1 or R2 connection fails | Check the account ID, database ID, API token permissions, R2 endpoint, bucket name, and R2 keys. |
| Railway cannot decrypt data | `BOT_ENCRYPTION_KEY` and `BOT_CONTROL_ID` must exactly match the desktop values. |
| Railway does not show new settings | Check the shared D1/R2 resources and control ID, wait for a sync, and keep one replica. |
| A ticket cannot be opened | Add and save at least one ticket staff member in the Tickets tab. |
| Voice or moderation actions fail | Check Discord permissions, enabled intents, bot role position, and channel access. |
| Messages or actions happen twice | Stop one process. Keep either the desktop bot or the Railway worker connected, not both. |

## Contributions and license

Keep feature code in its module and update this README when setup, commands, or controls change. Add a license file if you plan to publish or reuse this project; no license file is included here.
