"""Encrypted shared Firestore storage. No bot data is written to local JSON."""
import base64
import json
import os
import time
from datetime import datetime, timezone

import firebase_admin
from cryptography.fernet import Fernet, InvalidToken
from firebase_admin import credentials, firestore

DEFAULT_SETTINGS = {"token": "", "auto_connect": True, "allowed_roles": {}, "max_warnings_before_timeout": 5, "timeout_minutes": 10, "settings_revision": 1}


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
        return {**DEFAULT_SETTINGS, **self._open(snap.to_dict()["payload"])}

    def save_settings(self, settings):
        value = {**DEFAULT_SETTINGS, **settings}
        value["settings_revision"] = int(value.get("settings_revision", 0)) + 1
        self.control.set({"payload": self._seal(value), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
        return value

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

    def create_ticket(self, user_id, username, category, other_category, contact, problem):
        ref = self.control.collection("tickets").document()
        ticket = {"ticket_id": ref.id[:8].upper(), "user_id": str(user_id), "username": username,
                  "category": category, "other_category": other_category, "contact": contact,
                  "problem": problem, "status": "open", "created_at": datetime.now(timezone.utc).isoformat()}
        ref.set({"payload": self._seal(ticket), "created_at": firestore.SERVER_TIMESTAMP})
        return ticket

    def list_tickets(self, limit=200):
        records = []
        for snap in self.control.collection("tickets").order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit).stream():
            ticket = self._open(snap.to_dict()["payload"]); ticket["document_id"] = snap.id; records.append(ticket)
        return records

    def resolve_ticket(self, document_id, resolver, note):
        ref = self.control.collection("tickets").document(document_id)
        snap = ref.get()
        if not snap.exists:
            raise RuntimeError("Ticket no longer exists.")
        ticket = self._open(snap.to_dict()["payload"])
        ticket.update({"status": "resolved", "resolved_by": resolver, "resolution_note": note,
                       "resolved_at": datetime.now(timezone.utc).isoformat()})
        ref.set({"payload": self._seal(ticket), "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
        ticket["document_id"] = document_id
        return ticket
