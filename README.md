# Encrypted Discord Moderator: GUI + Railway

`bot.py` is the desktop GUI controller and `railway_bot.py` is the 24/7 headless Railway worker. They both run the exact same moderation implementation in `bot_core.py` and use the same encrypted Firebase/Firestore control document.

No configuration, warnings, token, or logs are stored in project JSON files. Every application value written to Firestore is Fernet encrypted before upload. Firestore only sees ciphertext in `payload` fields.

## Important design note

Run only **one live Discord gateway connection for each Discord bot token**. If the GUI and Railway use the same token at the same time, Discord can disconnect one process or both processes can receive an event. Use the GUI to configure Firebase, then run the Railway worker continuously. If two independent Discord bots are needed, create two bot applications/tokens and give each a different `BOT_CONTROL_ID`; they will be separate control groups.

## Firebase setup

1. Create a Firebase project at Firebase Console and create a **Cloud Firestore** database in production mode.
2. Open Firebase Project Settings → Service accounts → **Generate new private key**. Save it as `firebase-service-account.json` next to this project. Never commit or send it.
3. Copy `.env.example` to `.env` and fill in `FIREBASE_PROJECT_ID`, `FIREBASE_SERVICE_ACCOUNT_FILE`, and a new `BOT_ENCRYPTION_KEY`.
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

   Enter the Discord token and click **Save to Firebase**. Connect the GUI once to load its server roles, then select allowed roles and save them. Changes are encrypted, logged, and Railway sees them within 10 seconds.

Enable **Message Content Intent** and **Server Members Intent** in the Discord Developer Portal. The bot needs View Channels, Send Messages, Manage Messages, Read Message History, and Moderate Members.

## Railway 24/7 deployment

1. Create a new GitHub repository and push this project. Do not push `.env`, the Firebase service-account file, or any token.
2. In Railway: New Project → Deploy from GitHub Repo → select this repository.
3. Railway detects the included `Dockerfile` and starts `python railway_bot.py` as a persistent worker. No web GUI/server is required. Do not add a custom start command unless it is exactly `python railway_bot.py`.
4. In Railway Variables, add:

   - `BOT_ENCRYPTION_KEY`: the same Fernet key from the GUI.
   - `BOT_CONTROL_ID`: `production` (or the same value used in the GUI).
   - `FIREBASE_PROJECT_ID`: Firebase project ID.
   - `FIREBASE_SERVICE_ACCOUNT_JSON`: the complete contents of the service-account JSON, pasted as one JSON value. Base64 also works.

   `DISCORD_BOT_TOKEN` is optional: omit it to make Railway load the encrypted token from Firebase. If set, it is used only at startup.

5. In Railway Service Settings, set **Restart Policy** to **Always** and use a paid Railway plan. Railway's free/trial plans do not support the Always policy, so they cannot promise 24-hour uptime. Deploy. The worker reconnects to Discord after errors and Railway restarts the container if it stops.

## How synchronization works

The GUI saves the entire settings document as one encrypted Firestore payload and increments a revision. Railway polls it every 10 seconds. Warning counts use Firestore transactions so they stay shared and atomic. Both GUI and Railway write encrypted logs into the same Firestore subcollection.

## Support token workflow

Users run `/token` in Discord. The command opens a form for category (`Technical`, `Billing`, `Account`, or `Other`), other-category text, contact information, and a problem description. The submitted ticket is encrypted and saved in Firebase, and the user receives both an ephemeral confirmation and a Discord DM confirmation. A user can create only one ticket every five minutes, enforced atomically in Firebase across all running copies.

In the GUI, open **Support Tokens**, refresh the encrypted ticket list, select an open ticket, write a resolution note, and mark it resolved. The resolution is saved and the user receives a DM. The GUI also has a **Kick / Ban** page: select a connected server, enter a numeric member ID and reason, then kick or ban. These actions require the GUI bot to be connected and are rate-limited to one action per member every 10 seconds.

## What remains online on Railway

The Railway worker continuously runs all Discord-side functionality: link detection/deletion, warning and timeout logic, `/token` forms, ticket encryption and save, ticket-creation DM confirmations, Firebase settings polling, and encrypted logs. The desktop-only GUI pages (ticket list, Resolve, Kick, and Ban buttons) are intentionally local controls; when you use them, they save to the same Firebase data and/or call Discord through the connected GUI bot. Do not run the GUI connection and Railway with the same Discord token at the same time.

## Files

| File | Purpose |
| --- | --- |
| `bot.py` | Desktop GUI configuration/control program |
| `railway_bot.py` | Headless Railway worker entry point |
| `bot_core.py` | Shared Discord moderation logic |
| `firebase_store.py` | Encrypted Firestore settings, warnings, and logs |
| `Dockerfile` | Railway's explicit Python worker build and startup configuration |

## Security and recovery

Rotate the Discord token that was previously saved in old local JSON before deploying. Store the Fernet key and Firebase service account only in secret managers / Railway Variables. Losing the Fernet key means encrypted Firebase data cannot be recovered; use a new `BOT_CONTROL_ID` to start a clean control group.
