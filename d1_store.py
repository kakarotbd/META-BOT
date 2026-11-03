"""D1-backed state layer with R2 media inherited from CloudflareR2Store."""
import json, threading, os, random, time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from cloudflare_store import CloudflareR2Store

class CloudflareD1Store(CloudflareR2Store):
    def __init__(self):
        super().__init__()
        self.account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        self.d1_database_id = os.getenv("D1_DATABASE_ID", "").strip()
        self.d1_api_token = os.getenv("D1_API_TOKEN", "").strip()
        if not all((self.account_id, self.d1_database_id, self.d1_api_token)):
            raise RuntimeError("CLOUDFLARE_ACCOUNT_ID, D1_DATABASE_ID and D1_API_TOKEN are required.")
        self.d1_url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/d1/database/{self.d1_database_id}/query"
        self._local_locks = {}
        self._schema_ready = False

    def _query(self, sql, params=()):
        body = json.dumps({"sql": sql, "params": list(params)}).encode()
        # All D1 statements used by this store are replay-safe: reads, CREATE IF
        # NOT EXISTS, DELETE, or an UPSERT whose payload is fixed before sending.
        # In particular, retrying an XP write replays the same computed XP record,
        # rather than running the read/modify/write callback again.
        attempts = 4
        for attempt in range(attempts):
            request = Request(self.d1_url, data=body, method="POST", headers={"Authorization": f"Bearer {self.d1_api_token}", "Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=20) as response:
                    result = json.loads(response.read().decode())
            except (HTTPError, URLError) as exc:
                detail = exc.read().decode(errors="replace") if isinstance(exc, HTTPError) else str(exc)
                retryable = isinstance(exc, URLError) or (isinstance(exc, HTTPError) and (exc.code == 429 or 500 <= exc.code < 600))
                if retryable and attempt + 1 < attempts:
                    time.sleep(0.35 * (2 ** attempt) + random.uniform(0, 0.2))
                    continue
                raise RuntimeError(f"Cloudflare D1 request failed: {detail[:300]}") from exc
            if not result.get("success"):
                errors = result.get("errors", [])
                # Cloudflare's API can return an internal D1 failure in a 200
                # response envelope. Error 7429 in observed Railway logs is this
                # transient internal-error case.
                retryable = any(str(error.get("code", "")) in {"7429", "7500"} for error in errors if isinstance(error, dict))
                if retryable and attempt + 1 < attempts:
                    time.sleep(0.35 * (2 ** attempt) + random.uniform(0, 0.2))
                    continue
                raise RuntimeError(f"Cloudflare D1 query failed: {errors or result}")
            return result.get("result", [{}])[0]

    def _ensure_schema(self):
        if not self._schema_ready:
            self._query("CREATE TABLE IF NOT EXISTS records (collection TEXT NOT NULL, record_id TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (collection, record_id));")
            self._schema_ready = True

    def _put(self, collection, ident, value):
        self._ensure_schema()
        self._query("INSERT INTO records(collection,record_id,payload,updated_at) VALUES(?,?,?,?) ON CONFLICT(collection,record_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at", (str(collection), str(ident), self._seal(value), datetime.now(timezone.utc).isoformat()))

    def _get(self, collection, ident):
        self._ensure_schema()
        rows = self._query("SELECT payload FROM records WHERE collection=? AND record_id=?", (str(collection), str(ident))).get("results", [])
        return self._open(rows[0]["payload"]) if rows else None

    def _delete(self, collection, ident):
        self._ensure_schema()
        self._query("DELETE FROM records WHERE collection=? AND record_id=?", (str(collection), str(ident)))

    def _list(self, collection):
        self._ensure_schema()
        rows = self._query("SELECT record_id,payload,updated_at FROM records WHERE collection=?", (str(collection),)).get("results", [])
        result = []
        for row in rows:
            try:
                result.append((row["record_id"], self._open(row["payload"]), datetime.fromisoformat(row["updated_at"])))
            except (KeyError, ValueError):
                continue
        return result

    def _atomic(self, collection, ident, fn, default=None):
        key = f"{collection}:{ident}"
        lock = self._local_locks.setdefault(key, threading.Lock())
        with lock:
            current = self._get(collection, ident)
            return fn(current if current is not None else default)
