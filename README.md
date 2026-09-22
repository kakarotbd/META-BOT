# Encrypted Discord Moderator: GUI + Railway

`bot.py` is the desktop GUI controller and `railway_bot.py` is the 24/7 headless Railway worker. They both run the exact same moderation implementation in `bot_core.py` and use the same encrypted Firebase/Firestore control document.

No configuration, warnings, token, or logs are stored in project JSON files. Every application value written to Firestore is Fernet encrypted before upload. Firestore only sees ciphertext in `payload` fields.

## Important design note

Run only **one live Discord gateway connection for each Discord bot token**. If the GUI and Railway use the same token at the same time, Discord can disconnect one process or both processes can receive an event. Use the GUI to configure Firebase, then run the Railway worker continuously. If two independent Discord bots are needed, create two bot applications/tokens and give each a different `BOT_CONTROL_ID`; they will be separate control groups.

## Firebase setup

1. Create a Firebase project at Firebase Console and create a **Cloud Firestore** database in production mode.
2. Open Firebase Project Settings → Service accounts → **Generate new private key**. Save it as `firebase-service-account.json` next to this project. Never commit or send it.
3. Copy `.env.example` to `.env` and fill in `FIREBASE_PROJECT_ID`, `FIREBASE_SERVICE_ACCOUNT_FILE`, `ADMIN_USER_ID`, and a new `BOT_ENCRYPTION_KEY`.
4. Generate the encryption key once:

   ```powershell
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Keep this exact key private. Both the GUI computer and Railway need the same key, otherwise existing Firebase data cannot be decrypted.
5. Install packages:

   ```powershell
   python -m pip install -r requirements.txt
   ```

6. Run the GUI:

   ```powershell
   python bot.py
   ```

   Enter the Discord token and click **Save to Firebase**. Connect the GUI once to load its server roles, then select allowed roles in **Command Permissions** and save them. Those roles can use `/chat` (and share links); changes are encrypted, logged, and Railway sees them within 10 seconds.

Enable **Message Content Intent** and **Server Members Intent** in the Discord Developer Portal. The generated invite requests **Administrator** permission, which is required for temporary-room creation, permission changes, and moving members.

## Shared application configuration

`bot_app_config.txt` contains non-secret display metadata for both `bot.py` and `railway_bot.py`:

- `BOT_APP_NAME`: name shown by the desktop GUI and Railway startup log.
- `LOGO_PATH`: project-relative logo path. Put the Discord application logo at `assets/logo.png`; Docker copies this asset into Railway automatically. Railway is headless, so a missing logo only produces a startup warning; the worker still runs.
- `ADMIN_USER_ID`: protected administrator. Set it in local `.env` and Railway Variables; it is never read from this text file.
- Ticket moderators are selected per server in the GUI; new tickets rotate through them in the saved order.\r\n
Do not put a bot token, Firebase credential, or encryption key in this text file.
## Railway 24/7 deployment

1. Create a new GitHub repository and push this project. Do not push `.env`, the Firebase service-account file, or any token.
2. In Railway: New Project → Deploy from GitHub Repo → select this repository.
3. Railway detects the included `Dockerfile` and starts `python railway_bot.py` as a persistent worker. No web GUI/server is required. Do not add a custom start command unless it is exactly `python railway_bot.py`.
4. In Railway Variables, add:

   - `BOT_ENCRYPTION_KEY`: the same Fernet key from the GUI.
   - `BOT_CONTROL_ID`: `production` (or the same value used in the GUI).
   - `FIREBASE_PROJECT_ID`: Firebase project ID.
   - `ADMIN_USER_ID`: protected administrator Discord user ID; use the same value as local `.env`.
   - `FIREBASE_SERVICE_ACCOUNT_JSON`: the complete contents of the service-account JSON, pasted as one JSON value. Base64 also works.

   `DISCORD_BOT_TOKEN` is optional: omit it to make Railway load the encrypted token from Firebase. If set, it is used only at startup.

5. In Railway Service Settings, set **Restart Policy** to **Always** and use a paid Railway plan. Railway's free/trial plans do not support the Always policy, so they cannot promise 24-hour uptime. Deploy. The worker reconnects to Discord after errors and Railway restarts the container if it stops.

## Text Send (`/chat`)

In any server text channel, a member with a role selected in the GUI's **Command Permissions** tab can run `/chat`. It opens a private form; after entering up to 2,000 characters and pressing **Submit**, the bot posts that text in the same channel. Discord Markdown (for example `**bold**`, `*italic*`, and `` `code` ``) and pasted Unicode font text work in the form. The permission list is the encrypted Firebase setting shared by the GUI and Railway, and a short Firestore claim prevents duplicate posts during a brief gateway overlap.

## Activity leveling

In the GUI's **Leveling** tab, choose a server and the text channel where level-up cards should be posted, then enable the system and set its XP controls. Every meaningful message (at least 3 characters) earns the selected base XP plus a small length bonus, limited to one award per member per cooldown. Level 2 needs the selected base XP; later requirements follow `base × current level^multiplier`. When a member levels up, the selected channel receives a gold **Level Up!** embed mentioning them and showing their current Discord profile picture.

**Voice call XP** is part of the same system. Members currently connected to any non-AFK voice channel earn a configurable amount of XP on their own interval (default 10 XP every 60 seconds), counted toward the exact same level as text XP. Two safeguards are on by default and can be turned off per server: an award only happens when at least one other non-bot member is also in the call (blocks joining an empty room to farm XP), and self-deafened members are skipped (they aren't actually participating). Text and voice XP use independent cooldown timers internally, so being active in chat never delays voice XP or vice versa — both simply add up toward the member's level.

XP lives in encrypted Firestore and is awarded by a transaction, separately keyed per activity source (text vs. voice). This means the desktop GUI and Railway worker use the same XP, cooldown, and level record for both sources; even if they briefly overlap, one cooldown window for one source can only be claimed once, so leveling stays 100% consistent between the GUI and Railway. Configure with the GUI, then keep one gateway worker (normally Railway) online — the voice XP check runs automatically as part of that same worker, every 15 seconds, with no extra setup.

## Role notifications (scheduled role pings)

In the GUI's **Role Notifications** tab, pick a server, then for each scheduled notification choose a text channel, one or more roles, a time of day in 12-hour format (e.g. `8:00 PM`), and an optional image. Add as many notifications as you like per server, each with its own channel/roles/time/message/image. Each schedule uses the local timezone detected by the GUI and stores it with the notification, so Railway runs it in that same timezone.

Each notification also has a schedule, and exactly one of these three is picked per notification:
- **Everyday** (the default) — fires every day at the chosen time.
- **One or more specific weekdays** (Sat/Sun/Mon/Tue/Wed/Thu/Fri) — fires only on the checked day(s), every week.
- **A one-time date** (`YYYY-MM-DD`) — fires once, only on that calendar date, then never again.

The three options are mutually exclusive in the GUI: ticking Everyday clears and locks the weekday boxes and the date field; ticking any weekday clears and locks Everyday and the date field; typing a date clears and locks Everyday and every weekday box. Clear the field/uncheck the boxes to switch modes again.

At the configured time, on a day that matches the schedule, the bot pings the selected role(s) in the selected channel with your message (or a default "Reminder!" if left blank) and attaches the uploaded image, if any. The schedule lives in the same encrypted Firestore settings document as everything else, so Railway picks up changes within 10 seconds. Firing is claimed through a Firestore transaction keyed to the notification and the calendar date, so the ping is sent exactly once per matching day no matter which of the GUI or Railway happens to be the live gateway at that moment — they never double-post or race each other.

## Default roles (auto-role on join)

In the GUI's **Default Roles** tab, pick a server and select one or more roles in the list. Save, and from then on every member who joins that server automatically receives those role(s) the moment they join — no button, no command needed. This uses the exact same `on_member_join` event as the welcome message, and the role list is stored in the same encrypted Firestore settings document, so both the GUI and Railway apply the identical role set the instant either one is the connected gateway.

To also cover members who joined **before** this was set up, click **Apply to all existing members**. This requires the GUI's own bot connection to be online (Connection tab) — it walks every non-bot member of the selected server, gives anyone missing the saved role(s) the role(s) they're missing, and skips anyone who already has them. A progress bar and live `X%` label show how far it's gotten, and a short pause between each member keeps it well under Discord's rate limits on large servers. This is a one-time backfill; you can re-run it any time (e.g. after adding another default role) and it will only touch members who are still missing something.

## Owner protection

The Discord account `1022113587851960431` is hard-coded as protected in `bot_core.py` and can never be kicked or banned by this bot's own logic, regardless of how many rules it breaks: the automated link-spam warning/timeout system skips it entirely (same as an allowed-role holder), and the GUI's Kick/Ban page refuses to act on it even if a member ID is typed in manually. This is an application-level allowlist enforced only by this bot's code — it doesn't change what a human moderator can do by hand in Discord's own UI, and it applies identically whether the GUI or Railway is the connected gateway.

## How synchronization works

The GUI saves the entire settings document as one encrypted Firestore payload and increments a revision. Railway polls it every 10 seconds. Warning counts use Firestore transactions so they stay shared and atomic. Both GUI and Railway write encrypted logs into the same Firestore subcollection.

## Support token workflow

In the GUI's **Support Tokens** tab, select a server and a text channel, then save it as the ticket dashboard channel. The bot posts a persistent **🎫 Create Ticket** button there — no slash command required. A member clicks the button, types their own ticket category and problem description in the popup form, and submits. No ready-made categories or contact-information fields are shown. The submitted ticket is encrypted and saved in Firebase. The confirmation asks users to send friend requests to the listed support accounts so support can contact them on Discord. The server-side ephemeral confirmation contains the Cancel Ticket button; the user's inbox receives confirmation messages only. A user can create only one ticket every five minutes, enforced atomically in Firebase across all running copies.

The dashboard button is persistent: it keeps working after a GUI restart or a Railway redeploy without needing to be reposted. If you change the ticket channel in the GUI, the old button message is removed and a new one is posted in the new channel automatically, usually within 10 seconds.

As soon as a ticket is created, the bot sends a full copy of it — user, category, and problem — as a DM to the staff account `975756582170296382`, together with **✅ Resolved** and **🔒 Closed** buttons on that DM message. Clicking either opens a small note form:

- **Resolved** opens with a blank note. Whatever is written is sent to the ticket owner along with the resolved confirmation.
- **Closed** opens pre-filled with a default English apology note ("Sorry for the inconvenience. This ticket has been closed…"); it can be edited or left as-is before submitting, and that text is sent to the ticket owner.

Either action marks the ticket in encrypted Firebase, disables the buttons on the staff DM, and DMs the user with the note. The GUI's **Support Tokens** tab has matching **Mark selected ticket resolved + DM user** and **Close selected ticket + DM user** buttons that do exactly the same thing (the Close button also falls back to the same default apology note if the note field is left blank), so the same tickets can be handled from either the GUI or directly from Discord. All of it goes through the same encrypted Firestore document and the same status-change guard (a ticket can only leave "open" once), so the GUI and Railway worker never conflict — whichever one is connected to Discord at the time delivers the staff DM and processes the buttons, and both read/write the identical ticket record. The Resolved/Closed buttons on the staff DM are also persistent, so they keep working even after a GUI restart or Railway redeploy.

## Temporary voice rooms

In the GUI's **Temporary Voice** tab, select one voice channel per server as the join-to-create lobby and save it. When a member joins that lobby, the bot creates a room named after the member, moves them into it, and posts a voice-chat dashboard with emoji buttons for Lock, Unlock, Rename, and Member Limit. Only the room owner can use the dashboard. Room ownership, names, limits, locks, dashboard message ID, and lifecycle state are encrypted in Firebase; after a Railway restart the stored dashboard controls are restored. Empty rooms are deleted and their Firebase record is retained with a closed status.

## Welcome and goodbye

Use the GUI's **Welcome / Goodbye** tab to select separate text channels, custom messages, and images for member joins and leaves. The bot sends `Welcome` or `Goodbye`, then mentions the member, then displays the custom text and uploaded image. GUI compresses the selected image before encrypting it into Firebase; Railway reads the same configuration within 10 seconds and sends the image from Firebase.

In the GUI, open **Support Tokens**, refresh the encrypted ticket list, select an open ticket, write a resolution note, and mark it resolved. The resolution is saved and the user receives a DM. The GUI also has a **Kick / Ban** page: select a connected server, enter a numeric member ID and reason, then kick or ban. These actions require the GUI bot to be connected and are rate-limited to one action per member every 10 seconds.

After creating a ticket, the user receives a **Cancel Ticket** button in the confirmation. Pressing it opens a second confirmation step. Confirming marks the ticket as cancelled in encrypted Firebase, removes the Cancel Ticket button from the original message so it can't be clicked again, and sends a cancellation confirmation to the user's Discord inbox.

## Birthdays

Use the GUI's **Birthdays** tab to pick one text channel per server and (optionally) upload a birthday image. The bot posts a persistent **🎂 Add Birthday** button there. Clicking it opens a popup asking for day, month, and year; submitting saves the date and shows a private ("only you can see this") confirmation. Clicking **Add Birthday** again after a date is already saved shows a private warning asking whether to change it, instead of silently overwriting anything. On the day itself, the bot posts `Happy Birthday @member` in that same channel with an English wish message and the uploaded image, exactly once per member per year, even if GUI and Railway are both online. GUI and Railway share the same encrypted birthday data and image, so a date saved from either side stays in sync.

## What remains online on Railway

The Railway worker continuously runs all Discord-side functionality: link detection/deletion, warning and timeout logic, activity leveling (text and voice) and level-up announcements, the ticket dashboard button and forms, ticket encryption and save, ticket-creation DM confirmations, the birthday dashboard button, daily birthday wishes, Firebase settings polling, and encrypted logs. The desktop-only GUI pages (ticket list, Resolve, Kick, and Ban buttons) are intentionally local controls; when you use them, they save to the same Firebase data and/or call Discord through the connected GUI bot. Do not run the GUI connection and Railway with the same Discord token at the same time.

## Files

| File | Purpose |
| --- | --- |
| `bot.py` | Desktop GUI configuration/control program |
| `railway_bot.py` | Headless Railway worker entry point |
| `bot_core.py` | Shared Discord moderation logic (including role notifications and owner protection) |
| `firebase_store.py` | Encrypted Firestore settings, warnings, and logs |
| `Dockerfile` | Railway's explicit Python worker build and startup configuration |

## Security and recovery

Rotate the Discord token that was previously saved in old local JSON before deploying. Store the Fernet key and Firebase service account only in secret managers / Railway Variables. Losing the Fernet key means encrypted Firebase data cannot be recovered; use a new `BOT_CONTROL_ID` to start a clean control group.
