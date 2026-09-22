"""Encrypted shared Firestore storage. No bot data is written to local JSON."""
import base64
import json
import os
import time
from datetime import datetime, timezone, timedelta

import firebase_admin
from cryptography.fernet import Fernet, InvalidToken
from firebase_admin import credentials, firestore

# ``allowed_roles`` was the original shared list for both permissions below.
# It is retained only as a migration source for existing encrypted settings.
DEFAULT_SETTINGS = {"token": "", "auto_connect": True, "allowed_roles": {}, "command_allowed_roles": {}, "chat_allowed_roles": {}, "link_allowed_roles": {}, "drag_allowed_roles": {}, "ticket_channels": {}, "ticket_moderators": {}, "temp_voice_lobbies": {}, "welcome_goodbye": {}, "leveling": {}, "role_notifications": {}, "movie_announcements": {}, "default_roles": {}, "default_role_managed_ids": {}, "birthdays": {}, "counting_channels": {}, "max_warnings_before_timeout": 5, "timeout_minutes": 10, "settings_revision": 1}

class EncryptedFirestore:
    def __init__(self):
        key = os.getenv("BOT_ENCRYPTION_KEY", "").encode()
        self.control_id = os.getenv("BOT_CONTROL_ID", "production").strip()
        if not key:
            raise RuntimeError("BOT_ENCRYPTION_KEY is required.")
        try:
            self.cipher = Fernet(key)
        except ValueError as exc:
            raise RuntimeError("BOT_ENCRYPTION_KEY must be a valid Fernet key.") from exc
        self._initialize_firebase()
        self.db = firestore.client()
        self.control = self.db.collection("discord_bot_controls").document(self.control_id)
        self.logs = self.control.collection("logs")

    def _initialize_firebase(self):
        if firebase_admin._apps:
            return
        raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
        filename = os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE", "firebase-service-account.json")
        project_id = os.getenv("FIREBASE_PROJECT_ID", "").strip() or None
        if raw:
            try:
                info = json.loads(raw)
            except json.JSONDecodeError:
                info = json.loads(base64.b64decode(raw).decode("utf-8"))
            cred = credentials.Certificate(info)
        elif os.path.exists(filename):
            cred = credentials.Certificate(filename)
        else:
            raise RuntimeError("Set FIREBASE_SERVICE_ACCOUNT_JSON or provide firebase-service-account.json.")
        firebase_admin.initialize_app(cred, {"projectId": project_id} if project_id else None)

    def _seal(self, value):
        return self.cipher.encrypt(json.dumps(value, separators=(",", ":")).encode()).decode()

    def _open(self, value):
        try:
            return json.loads(self.cipher.decrypt(value.encode()).decode())
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Cannot decrypt Firebase data. Check BOT_ENCRYPTION_KEY and BOT_CONTROL_ID.") from exc

    def get_settings(self):
        snap = self.control.get()
        if not snap.exists or not snap.to_dict().get("payload"):
            self.control.set({"payload": self._seal(DEFAULT_SETTINGS), "updated_at": firestore.SERVER_TIMESTAMP})
            return dict(DEFAULT_SETTINGS)
        return self._normalize_settings(self._open(snap.to_dict()["payload"]))

    @staticmethod
    def _normalize_settings(settings):
        """Migrate old shared permissions while preserving all existing access."""
        value = {**DEFAULT_SETTINGS, **settings}
        legacy_roles = value.get("allowed_roles", {})
        if "command_allowed_roles" not in settings:
            value["command_allowed_roles"] = dict(legacy_roles)
        if "chat_allowed_roles" not in settings:
            value["chat_allowed_roles"] = dict(legacy_roles)
        # Before Chat Permissions became /chat access, this map held the link
        # allow-list. Keep those selected roles when introducing Link Permissions.
        if "link_allowed_roles" not in settings:
            value["link_allowed_roles"] = dict(settings.get("chat_allowed_roles", legacy_roles))
        return value

    def save_settings(self, settings):
        value = self._normalize_settings(settings)
        value["settings_revision"] = int(value.get("settings_revision", 0)) + 1
        self.control.set({"payload": self._seal(value), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
        return value

    def save_permission_roles(self, permission_name, guild_id, role_ids):
        """Atomically update one permission list without overwriting other GUI changes."""
        if permission_name not in {"command_allowed_roles", "chat_allowed_roles", "link_allowed_roles", "drag_allowed_roles"}:
            raise ValueError("Unknown permission setting.")
        guild_id = str(guild_id)
        cleaned_ids = [str(role_id) for role_id in role_ids]
        transaction = self.db.transaction()

        @firestore.transactional
        def update(transaction):
            snap = self.control.get(transaction=transaction)
            current = self._open(snap.to_dict()["payload"]) if snap.exists and snap.to_dict().get("payload") else {}
            value = self._normalize_settings(current)
            permission_map = dict(value.get(permission_name, {}))
            permission_map[guild_id] = cleaned_ids
            value[permission_name] = permission_map
            value["settings_revision"] = int(value.get("settings_revision", 0)) + 1
            transaction.set(self.control, {"payload": self._seal(value), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
            return value

        return update(transaction)

    def save_ticket_moderators(self, guild_id, user_ids):
        """Save ordered ticket moderators and reset that server's round-robin cursor."""
        guild_id = str(guild_id)
        cleaned_ids = list(dict.fromkeys(str(user_id) for user_id in user_ids if str(user_id).isdigit()))
        transaction = self.db.transaction()
        @firestore.transactional
        def update(transaction):
            snap = self.control.get(transaction=transaction)
            current = self._open(snap.to_dict()["payload"]) if snap.exists and snap.to_dict().get("payload") else {}
            value = self._normalize_settings(current); moderators = dict(value.get("ticket_moderators", {}))
            if cleaned_ids: moderators[guild_id] = cleaned_ids
            else: moderators.pop(guild_id, None)
            value["ticket_moderators"] = moderators; value["settings_revision"] = int(value.get("settings_revision", 0)) + 1
            transaction.set(self.control, {"payload": self._seal(value), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
            transaction.set(self.control.collection("ticket_assignment_cursors").document(guild_id), {"payload": self._seal({"next_index": 0}), "updated_at": firestore.SERVER_TIMESTAMP})
            return value
        return update(transaction)
    def save_default_roles(self, guild_id, role_ids):
        """Atomically save active Auto Roles and their managed-role history."""
        guild_id = str(guild_id); cleaned_ids = [str(role_id) for role_id in role_ids]
        transaction = self.db.transaction()
        @firestore.transactional
        def update(transaction):
            snap = self.control.get(transaction=transaction)
            current = self._open(snap.to_dict()["payload"]) if snap.exists and snap.to_dict().get("payload") else {}
            value = self._normalize_settings(current); defaults = dict(value.get("default_roles", {})); managed = dict(value.get("default_role_managed_ids", {}))
            previous = [str(role_id) for role_id in defaults.get(guild_id, [])]
            history = set(map(str, managed.get(guild_id, previous))); history.update(cleaned_ids)
            if cleaned_ids: defaults[guild_id] = cleaned_ids
            else: defaults.pop(guild_id, None)
            if history: managed[guild_id] = sorted(history)
            value["default_roles"], value["default_role_managed_ids"] = defaults, managed
            value["settings_revision"] = int(value.get("settings_revision", 0)) + 1
            transaction.set(self.control, {"payload": self._seal(value), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
            return value
        return update(transaction)

    def increment_warning(self, user_id):
        ref = self.control.collection("warnings").document(str(user_id))
        transaction = self.db.transaction()
        @firestore.transactional
        def update(transaction):
            snap = ref.get(transaction=transaction)
            value = self._open(snap.to_dict()["payload"]) if snap.exists else {"count": 0}
            value["count"] = int(value.get("count", 0)) + 1
            transaction.set(ref, {"payload": self._seal(value), "updated_at": firestore.SERVER_TIMESTAMP})
            return value["count"]
        return update(transaction)

    def reset_warning(self, user_id):
        self.control.collection("warnings").document(str(user_id)).set({"payload": self._seal({"count": 0}), "updated_at": firestore.SERVER_TIMESTAMP})

    def add_log(self, source, message):
        self.logs.add({"payload": self._seal({"source": source, "message": message, "at": datetime.now(timezone.utc).isoformat()}), "created_at": firestore.SERVER_TIMESTAMP})

    def recent_logs(self, limit=100):
        entries = []
        for snap in self.logs.order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit).stream():
            entries.append(self._open(snap.to_dict()["payload"]))
        return list(reversed(entries))

    def delete_logs_older_than(self, days=30):
        """Permanently delete shared log documents older than the requested age."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        deleted = 0
        batch = self.db.batch()
        for snap in self.logs.where("created_at", "<", cutoff).stream():
            batch.delete(snap.reference); deleted += 1
            if deleted % 450 == 0:
                batch.commit(); batch = self.db.batch()
        if deleted % 450:
            batch.commit()
        return deleted

    def claim_rate_limit(self, action, subject_id, seconds):
        """Atomically reserve an action. Returns False when it is still cooling down."""
        ref = self.control.collection("rate_limits").document(f"{action}_{subject_id}")
        transaction = self.db.transaction()
        @firestore.transactional
        def claim(transaction):
            snap = ref.get(transaction=transaction)
            value = self._open(snap.to_dict()["payload"]) if snap.exists else {"last": 0}
            now = time.time()
            if now - float(value.get("last", 0)) < seconds:
                return False
            transaction.set(ref, {"payload": self._seal({"last": now}), "updated_at": firestore.SERVER_TIMESTAMP})
            return True
        return claim(transaction)

    def submit_counting_number(self, guild_id, channel_id, user_id, message_id, submitted_number):
        """Atomically validate one counting-game message across GUI and Railway.

        The message ID makes gateway overlap idempotent: a second process seeing the
        same Discord message receives ``duplicate`` instead of advancing/resetting
        the counter a second time.
        """
        guild_id, channel_id, user_id, message_id = map(str, (guild_id, channel_id, user_id, message_id))
        ref = self.control.collection("counting_state").document(guild_id)
        transaction = self.db.transaction()

        @firestore.transactional
        def submit(transaction):
            snap = ref.get(transaction=transaction)
            state = self._open(snap.to_dict()["payload"]) if snap.exists and snap.to_dict().get("payload") else {}
            if state.get("channel_id") != channel_id or state.get("rule_version") != 2:
                state = {"channel_id": channel_id, "rule_version": 2, "expected": 1, "last_user_id": "", "last_message_id": ""}
            if state.get("last_message_id") == message_id:
                return {"duplicate": True}
            expected = int(state.get("expected", 1))
            correct = submitted_number == expected
            # A wrong entry never resets progress; the same expected number remains next.
            next_expected = expected + 1 if correct else expected
            updated = {
                "channel_id": channel_id, "rule_version": 2, "expected": next_expected,
                "last_user_id": user_id if correct else "", "last_correct_number": submitted_number if correct else 0, "last_message_id": message_id,
            }
            transaction.set(ref, {"payload": self._seal(updated), "updated_at": firestore.SERVER_TIMESTAMP})
            return {"duplicate": False, "correct": correct, "expected": expected, "next_expected": next_expected}

        return submit(transaction)
    def reset_counting_state(self, guild_id, channel_id):
        """Persistently reset one configured counting channel to expect 1."""
        self.control.collection("counting_state").document(str(guild_id)).set({
            "payload": self._seal({"channel_id": str(channel_id), "rule_version": 2, "expected": 1, "last_user_id": "", "last_correct_number": 0, "last_message_id": ""}),
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
    def create_ticket(self, guild_id, user_id, username, category, other_category, contact, problem):
        """Create one ticket and atomically assign the next configured moderator."""
        guild_id = str(guild_id); ref = self.control.collection("tickets").document(); cursor_ref = self.control.collection("ticket_assignment_cursors").document(guild_id)
        transaction = self.db.transaction()
        @firestore.transactional
        def create(transaction):
            snap = self.control.get(transaction=transaction)
            current = self._open(snap.to_dict()["payload"]) if snap.exists and snap.to_dict().get("payload") else {}
            moderators = list(dict.fromkeys(map(str, self._normalize_settings(current).get("ticket_moderators", {}).get(guild_id, []))))
            if not moderators: raise RuntimeError("No ticket moderators are configured for this server.")
            cursor_snap = cursor_ref.get(transaction=transaction)
            cursor = self._open(cursor_snap.to_dict()["payload"]) if cursor_snap.exists and cursor_snap.to_dict().get("payload") else {"next_index": 0}
            index = int(cursor.get("next_index", 0)) % len(moderators)
            ticket = {"ticket_id": ref.id[:8].upper(), "guild_id": guild_id, "user_id": str(user_id), "username": username, "category": category, "other_category": other_category, "contact": contact, "problem": problem, "assigned_moderator_id": moderators[index], "status": "open", "created_at": datetime.now(timezone.utc).isoformat()}
            transaction.set(ref, {"payload": self._seal(ticket), "created_at": firestore.SERVER_TIMESTAMP})
            transaction.set(cursor_ref, {"payload": self._seal({"next_index": (index + 1) % len(moderators)}), "updated_at": firestore.SERVER_TIMESTAMP})
            return ticket
        ticket = create(transaction); ticket["document_id"] = ref.id
        return ticket
    def list_tickets(self, limit=200):
        records = []
        for snap in self.control.collection("tickets").order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit).stream():
            ticket = self._open(snap.to_dict()["payload"]); ticket["document_id"] = snap.id; records.append(ticket)
        return records

    def get_ticket(self, document_id):
        snap = self.control.collection("tickets").document(document_id).get()
        if not snap.exists or not snap.to_dict().get("payload"):
            return None
        ticket = self._open(snap.to_dict()["payload"])
        ticket["document_id"] = document_id
        return ticket

    def update_ticket_fields(self, document_id, updates):
        """Merge arbitrary fields into an existing ticket record (e.g. staff DM message location)."""
        ref = self.control.collection("tickets").document(document_id)
        snap = ref.get()
        if not snap.exists:
            raise RuntimeError("Ticket no longer exists.")
        ticket = self._open(snap.to_dict()["payload"])
        ticket.update(updates)
        ref.set({"payload": self._seal(ticket), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
        ticket["document_id"] = document_id
        return ticket

    def set_ticket_status(self, document_id, status, resolver, note):
        """Mark a ticket resolved or closed. Only one of GUI/staff-DM can win the transition from 'open'."""
        ref = self.control.collection("tickets").document(document_id)
        snap = ref.get()
        if not snap.exists:
            raise RuntimeError("Ticket no longer exists.")
        ticket = self._open(snap.to_dict()["payload"])
        if ticket.get("status") != "open":
            raise RuntimeError(f"This ticket is already {ticket.get('status')}.")
        ticket.update({"status": status, "resolved_by": resolver, "resolution_note": note,
                       "resolved_at": datetime.now(timezone.utc).isoformat()})
        ref.set({"payload": self._seal(ticket), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
        ticket["document_id"] = document_id
        return ticket

    def resolve_ticket(self, document_id, resolver, note):
        """Backward-compatible alias for set_ticket_status(..., 'resolved', ...)."""
        return self.set_ticket_status(document_id, "resolved", resolver, note)

    def delete_ticket(self, document_id):
        ref = self.control.collection("tickets").document(document_id)
        if not ref.get().exists:
            raise RuntimeError("Ticket no longer exists.")
        ref.delete()

    def cancel_ticket(self, document_id, user_id):
        ref = self.control.collection("tickets").document(document_id)
        snap = ref.get()
        if not snap.exists:
            raise RuntimeError("Ticket no longer exists.")
        ticket = self._open(snap.to_dict()["payload"])
        if str(ticket["user_id"]) != str(user_id):
            raise RuntimeError("Only the ticket owner can cancel this ticket.")
        if ticket.get("status") != "open":
            raise RuntimeError(f"This ticket is already {ticket.get('status')}.")
        ticket.update({"status": "cancelled", "cancelled_at": datetime.now(timezone.utc).isoformat()})
        ref.set({"payload": self._seal(ticket), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
        ticket["document_id"] = document_id
        return ticket

    def save_temp_voice_room(self, room):
        """Persist room ownership so a Railway/GUI restart keeps room controls working."""
        room = dict(room)
        room["updated_at"] = datetime.now(timezone.utc).isoformat()
        room_id = f"{room['guild_id']}_{room['channel_id']}"
        self.control.collection("temp_voice_rooms").document(room_id).set(
            {"payload": self._seal(room), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
        )
        return room

    def get_temp_voice_room(self, guild_id, channel_id):
        snap = self.control.collection("temp_voice_rooms").document(f"{guild_id}_{channel_id}").get()
        if not snap.exists or not snap.to_dict().get("payload"):
            return None
        return self._open(snap.to_dict()["payload"])

    def list_active_temp_voice_rooms(self, limit=500):
        rooms = []
        for snap in self.control.collection("temp_voice_rooms").limit(limit).stream():
            room = self._open(snap.to_dict()["payload"])
            if room.get("status") == "active":
                rooms.append(room)
        return rooms

    def close_temp_voice_room(self, guild_id, channel_id):
        ref = self.control.collection("temp_voice_rooms").document(f"{guild_id}_{channel_id}")
        snap = ref.get()
        if not snap.exists:
            return
        room = self._open(snap.to_dict()["payload"])
        room.update({"status": "closed", "closed_at": datetime.now(timezone.utc).isoformat()})
        ref.set({"payload": self._seal(room), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)

    def claim_temp_voice_creation(self, guild_id, lobby_id, user_id, seconds=30):
        """Let only one GUI/Railway process create a room for one lobby join."""
        ref = self.control.collection("temp_voice_creation_locks").document(f"{guild_id}_{lobby_id}_{user_id}")
        transaction = self.db.transaction()

        @firestore.transactional
        def claim(transaction):
            snap = ref.get(transaction=transaction)
            value = self._open(snap.to_dict()["payload"]) if snap.exists else {"last": 0}
            now = time.time()
            if now - float(value.get("last", 0)) < seconds:
                return False
            transaction.set(ref, {"payload": self._seal({"last": now}), "updated_at": firestore.SERVER_TIMESTAMP})
            return True

        return claim(transaction)

    def release_temp_voice_creation(self, guild_id, lobby_id, user_id):
        ref = self.control.collection("temp_voice_creation_locks").document(f"{guild_id}_{lobby_id}_{user_id}")
        ref.set({"payload": self._seal({"last": 0}), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)

    def claim_role_notification_fire(self, entry_id, date_str):
        """Atomically claim the single daily send for a scheduled role notification.

        Returns True the first time this is called for a given entry_id/date_str
        (across GUI and Railway); returns False on every later call for that
        same day, so the reminder is posted exactly once even if both processes
        are checking schedules at the same moment.
        """
        ref = self.control.collection("role_notification_state").document(str(entry_id))
        transaction = self.db.transaction()

        @firestore.transactional
        def claim(transaction):
            snap = ref.get(transaction=transaction)
            value = self._open(snap.to_dict()["payload"]) if snap.exists and snap.to_dict().get("payload") else {}
            if value.get("last_fired_date") == date_str:
                return False
            transaction.set(ref, {"payload": self._seal({"last_fired_date": date_str}), "updated_at": firestore.SERVER_TIMESTAMP})
            return True

        return claim(transaction)

    def claim_movie_announcement_fire(self, entry_id, date_str):
        """Atomically claim one movie announcement across GUI and Railway."""
        ref = self.control.collection("movie_announcement_state").document(str(entry_id))
        transaction = self.db.transaction()

        @firestore.transactional
        def claim(transaction):
            snap = ref.get(transaction=transaction)
            value = self._open(snap.to_dict()["payload"]) if snap.exists and snap.to_dict().get("payload") else {}
            if value.get("fired_date") == date_str:
                return False
            transaction.set(ref, {"payload": self._seal({"fired_date": date_str}), "updated_at": firestore.SERVER_TIMESTAMP})
            return True

        return claim(transaction)

    def award_activity_xp(self, guild_id, user_id, username, avatar_url, xp, cooldown_seconds, level_base, level_multiplier, source="text"):
        """Atomically award activity XP once per cooldown across GUI and Railway.

        `source` ("text" or "voice") tracks its own cooldown timestamp so chatting and
        being in a voice call never block each other, while both still add to the same
        shared total_xp/level for the member. The transaction guarantees that even if
        the GUI and Railway briefly overlap, only one award per cooldown window per
        source is ever applied.
        """
        ref = self.control.collection("level_users").document(f"{guild_id}_{user_id}")
        transaction = self.db.transaction()
        # "text" keeps the original field name for backward compatibility with records
        # written before voice leveling existed.
        cooldown_field = "last_xp_at" if source == "text" else f"last_{source}_xp_at"

        @firestore.transactional
        def award(transaction):
            snap = ref.get(transaction=transaction)
            value = self._open(snap.to_dict()["payload"]) if snap.exists else {
                "guild_id": str(guild_id), "user_id": str(user_id), "level": 1, "xp": 0, "total_xp": 0,
                "last_xp_at": 0, "last_voice_xp_at": 0,
            }
            now = time.time()
            if now - float(value.get(cooldown_field, 0)) < cooldown_seconds:
                return None
            old_level = int(value.get("level", 1))
            value["xp"] = int(value.get("xp", 0)) + int(xp)
            value["total_xp"] = int(value.get("total_xp", 0)) + int(xp)
            value[cooldown_field] = now
            value["username"], value["avatar_url"] = username, avatar_url
            while value["xp"] >= int(round(level_base * (int(value["level"]) ** level_multiplier))):
                value["xp"] -= int(round(level_base * (int(value["level"]) ** level_multiplier)))
                value["level"] = int(value["level"]) + 1
            value["updated_at"] = datetime.now(timezone.utc).isoformat()
            transaction.set(ref, {"payload": self._seal(value), "updated_at": firestore.SERVER_TIMESTAMP})
            return {"level": value["level"], "old_level": old_level, "xp": value["xp"], "leveled_up": value["level"] > old_level}
        return award(transaction)

    def save_ticket_dashboard(self, guild_id, channel_id, message_id):
        """Remember where the persistent 'Create Ticket' button lives so restarts don't duplicate it."""
        value = {"channel_id": str(channel_id), "message_id": str(message_id)}
        self.control.collection("ticket_dashboards").document(str(guild_id)).set(
            {"payload": self._seal(value), "updated_at": firestore.SERVER_TIMESTAMP}
        )
        return value

    def get_ticket_dashboard(self, guild_id):
        snap = self.control.collection("ticket_dashboards").document(str(guild_id)).get()
        if not snap.exists or not snap.to_dict().get("payload"):
            return None
        return self._open(snap.to_dict()["payload"])

    def save_welcome_goodbye_image(self, image_id, content_type, data):
        """Store a compressed GUI-selected image encrypted in Firestore for Railway to reuse."""
        if len(data) > 500_000:
            raise ValueError("Image is too large after compression; use an image under 500 KB.")
        value = {"content_type": content_type, "data": base64.b64encode(data).decode()}
        self.control.collection("welcome_goodbye_images").document(image_id).set(
            {"payload": self._seal(value), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
        )
        return image_id

    def get_welcome_goodbye_image(self, image_id):
        if not image_id:
            return None
        snap = self.control.collection("welcome_goodbye_images").document(str(image_id)).get()
        if not snap.exists:
            return None
        value = self._open(snap.to_dict()["payload"])
        return value["content_type"], base64.b64decode(value["data"])

    def save_notification_image(self, image_id, content_type, data):
        """Store a compressed GUI-selected role-notification image encrypted in Firestore for Railway to reuse."""
        if len(data) > 500_000:
            raise ValueError("Image is too large after compression; use an image under 500 KB.")
        value = {"content_type": content_type, "data": base64.b64encode(data).decode()}
        self.control.collection("notification_images").document(image_id).set(
            {"payload": self._seal(value), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
        )
        return image_id

    def get_notification_image(self, image_id):
        if not image_id:
            return None
        snap = self.control.collection("notification_images").document(str(image_id)).get()
        if not snap.exists:
            return None
        value = self._open(snap.to_dict()["payload"])
        return value["content_type"], base64.b64decode(value["data"])

    def save_birthday(self, guild_id, user_id, username, day, month, year):
        """Save (or overwrite) one member's birthday for a guild."""
        ref = self.control.collection("birthdays").document(f"{guild_id}_{user_id}")
        payload = {
            "guild_id": str(guild_id), "user_id": str(user_id), "username": username,
            "day": int(day), "month": int(month), "year": int(year) if year else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        ref.set({"payload": self._seal(payload), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
        return payload

    def get_birthday(self, guild_id, user_id):
        snap = self.control.collection("birthdays").document(f"{guild_id}_{user_id}").get()
        if not snap.exists or not snap.to_dict().get("payload"):
            return None
        return self._open(snap.to_dict()["payload"])

    def list_birthdays(self, guild_id):
        """All saved birthdays for one guild, used by the daily birthday check."""
        records = []
        for snap in self.control.collection("birthdays").stream():
            data = snap.to_dict()
            if not data or not data.get("payload"):
                continue
            record = self._open(data["payload"])
            if str(record.get("guild_id")) == str(guild_id):
                records.append(record)
        return records

    def claim_birthday_fire(self, guild_id, user_id, date_str):
        """Atomically claim the single yearly birthday wish for a member on a given date.

        Same one-shot pattern as claim_role_notification_fire: returns True only the
        first time this is called for a given guild/user/date across GUI and Railway,
        so the wish is posted exactly once even if both processes check at once.
        """
        ref = self.control.collection("birthday_fire_state").document(f"{guild_id}_{user_id}")
        transaction = self.db.transaction()

        @firestore.transactional
        def claim(transaction):
            snap = ref.get(transaction=transaction)
            value = self._open(snap.to_dict()["payload"]) if snap.exists and snap.to_dict().get("payload") else {}
            if value.get("last_fired_date") == date_str:
                return False
            transaction.set(ref, {"payload": self._seal({"last_fired_date": date_str}), "updated_at": firestore.SERVER_TIMESTAMP})
            return True

        return claim(transaction)

    def save_birthday_dashboard(self, guild_id, channel_id, message_id):
        """Remember where the persistent 'Add Birthday' button lives so restarts don't duplicate it."""
        value = {"channel_id": str(channel_id), "message_id": str(message_id)}
        self.control.collection("birthday_dashboards").document(str(guild_id)).set(
            {"payload": self._seal(value), "updated_at": firestore.SERVER_TIMESTAMP}
        )
        return value

    def get_birthday_dashboard(self, guild_id):
        snap = self.control.collection("birthday_dashboards").document(str(guild_id)).get()
        if not snap.exists or not snap.to_dict().get("payload"):
            return None
        return self._open(snap.to_dict()["payload"])

    def save_birthday_image(self, image_id, content_type, data):
        """Store a compressed GUI-selected birthday image encrypted in Firestore for Railway to reuse."""
        if len(data) > 500_000:
            raise ValueError("Image is too large after compression; use an image under 500 KB.")
        value = {"content_type": content_type, "data": base64.b64encode(data).decode()}
        self.control.collection("birthday_images").document(image_id).set(
            {"payload": self._seal(value), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
        )
        return image_id

    def get_birthday_image(self, image_id):
        if not image_id:
            return None
        snap = self.control.collection("birthday_images").document(str(image_id)).get()
        if not snap.exists:
            return None
        value = self._open(snap.to_dict()["payload"])
        return value["content_type"], base64.b64decode(value["data"])
