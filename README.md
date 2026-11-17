# MetaBot

MetaBot is a Discord community bot with a Windows desktop control panel. Configure it in the panel and keep it online with the included Railway worker. Both use the same Cloudflare database and file storage.

![MetaBot animated banner](docs/metabot-banner.gif)

> **Security:** An earlier `.env.example` contained live-looking credentials. If you used or published those values, revoke or rotate the Discord and Cloudflare credentials before deployment. The template now contains placeholders only.

## Features

| Area | What MetaBot does |
| --- | --- |
| Custom GIF reactions | Add your own member-targeting slash commands, such as `/kiss` and `/slap`, with GIF links in the desktop GUI. |
| Chat and link controls | Let selected roles use `/chat`; choose which roles can post links. Discord invite links are always blocked. |
| Tickets | Post a ticket panel, collect a category and issue, create a private support channel, assign or transfer staff, add notes, resolve or close, and collect member ratings. Staff can see their rating summary in DMs. Unanswered tickets receive staff reminders. |
| Birthdays | Let members add or change their birthday privately; send birthday wishes to a chosen channel and mention selected roles. |
| Greetings and roles | Send welcome and goodbye messages, optionally with a saved image; give selected default roles to new members and apply them to current members. |
| Leveling | Award XP for eligible text and voice activity, post level-up messages, and show a top 10 image leaderboard. Configure XP amounts and cooldowns; voice XP can require another person and skip deafened users. |
| Counting | Set a counting channel, keep the next-number sequence, and reset its count from the GUI. |
| Temporary voice | Create a private room when a member joins the chosen lobby. The owner can lock or unlock it, rename it, set a member limit, and invite someone. Empty rooms are removed. |
| Member moves | Allow selected roles to use `/move` to pick active voice members and move them. The GUI also controls who may use drag permissions. |
| Scheduled posts | Send role notifications every day, on selected weekdays, or on a selected date. Schedule a one-time movie announcement with a title, text, poster, and role mentions. |
| Moderation | Use the GUI to kick or ban a member by Discord ID and reason. The configured administrator cannot be kicked or banned by this bot. |
| Blocked words | Delete messages that match configured blocked words. Repeated violations can lead to an automatic ban after five strikes. |
| Bot anti-spam | Check server messages from bots for mass mentions, mention floods, rapid bursts, and repeated messages. The bot attempts a ban and alerts the configured administrator. It cannot see private DMs. |
| Control panel and storage | View connection status and live logs, check database usage, export/import a JSON backup, and share saved settings with the Railway worker. |

The Windows desktop panel has these tabs: **Overview, GIF Commands, Chat, Link Send, Drag Permissions, Tickets, Voice, Greetings, Leveling, Counting, Birthdays, Notifications, Movies, Auto Roles, Moderation, Word Block,** and **Live Logs**. Use **All Controls** to jump to a tab. The Overview also has database usage and backup export/import controls.

## Every Discord command

These are the built-in commands registered with Discord:

| Command | What it does | Who can use it |
| --- | --- | --- |
| `/help` | Opens a private guide with pages about bot features. | Server members |
| `/chat` | Opens a form to post a message in the current channel. | Members with a role allowed in the GUI's **Chat** tab |
| `/leaderboard` | Posts an image of the top 10 members by total XP. | Server members |
| `/move` | Opens a private member and voice-channel picker to move members. | Members with a role allowed in **Drag Permissions**; the bot needs Move Members permission |
| Custom slash commands, for example `/kiss` or `/slap` | Picks a target member and posts one of the GIFs saved for that command. | Server members |

There are no built-in `!` prefix commands. The desktop panel also has GUI actions such as moderation, database usage, log loading, and backup export/import; those are buttons, not Discord commands.

### Add `/kiss`, `/slap`, or another GIF command

1. Open the desktop panel, connect the bot, and select your server.
2. Open **GIF Commands**.
3. Enter a lowercase command name, such as `kiss` or `slap`.
4. Paste one or more GIF URLs, with one URL on each line.
5. Select **Save / update command**.
6. In Discord, run `/kiss` or `/slap` and choose the member when Discord asks for a target.

Use only lowercase letters, numbers, `_`, or `-` in command names (up to 32 characters). Add at least one `http://` or `https://` GIF URL. The bot cycles through the saved GIFs in shuffled order. Discord may take a few seconds to show a new command; the Railway worker syncs saved settings within about 10 seconds.

Ticket and birthday flows use buttons posted in the channels configured by the server admin: **Create Ticket** and **Add Birthday**. Temporary voice rooms also show owner control buttons. These are interactive buttons, not slash commands.

Configured ticket staff can DM the bot `start` or `star` to get their support rating summary. Other users do not get this summary.

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
