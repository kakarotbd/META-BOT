"""D1-backed state layer with R2 media inherited from CloudflareR2Store."""
import json, threading, os, random, time
import base64
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from storage.cloudflare_store import CloudflareR2Store

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

    def get_database_usage(self):
        request = Request(f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/d1/database/{self.d1_database_id}?fields=file_size", headers={"Authorization": f"Bearer {self.d1_api_token}"})
        try:
            with urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode())
            if result.get("success") and result.get("result",{}).get("file_size") is not None:
                return int(result["result"]["file_size"]), False
        except Exception:
            pass
        self._ensure_schema()
        row=self._query("SELECT COALESCE(SUM(LENGTH(collection)+LENGTH(record_id)+LENGTH(payload)+LENGTH(updated_at)),0) AS bytes FROM records").get("results",[{}])[0]
        return int(row.get("bytes",0)), True

    def export_snapshot(self, progress=None):
        self._ensure_schema()
        rows=[]; offset=0; page_size=500
        while True:
            page_rows=self._query("SELECT collection,record_id,payload,updated_at FROM records ORDER BY collection,record_id LIMIT ? OFFSET ?",(page_size,offset)).get("results",[])
            rows.extend(page_rows)
            if len(page_rows)<page_size:break
            offset+=page_size
        keys=[]; token=None; prefix=f"{self.root}/"
        while True:
            args={"Bucket":self.bucket,"Prefix":prefix}
            if token: args["ContinuationToken"]=token
            page=self.s3.list_objects_v2(**args)
            keys.extend(item["Key"] for item in page.get("Contents",[]))
            if not page.get("IsTruncated"): break
            token=page.get("NextContinuationToken")
        objects=[]; total=max(1,len(rows)+len(keys)); done=0
        for row in rows:
            done+=1
            if progress:progress(done,total,"Exporting D1 records")
        for key in keys:
            stored=self.s3.get_object(Bucket=self.bucket,Key=key)
            data=stored["Body"].read()
            objects.append({"key":key,"data":base64.b64encode(data).decode("ascii"),"content_type":stored.get("ContentType"),"cache_control":stored.get("CacheControl"),"metadata":stored.get("Metadata",{})})
            done+=1
            if progress:progress(done,total,"Exporting R2 files")
        return {"format":"meta-bot-backup","version":1,"control_id":self.control_id,"d1_records":rows,"r2_objects":objects}

    def import_snapshot_item(self, kind, item):
        if kind=="d1":
            self._ensure_schema()
            collection,record_id=str(item["collection"]),str(item["record_id"])
            payload=str(item["payload"])
            if collection=="settings" and record_id=="main":
                restored=self._open(payload)
                for retired_key in ("monitored_users", "discipline", "token"):
                    restored.pop(retired_key, None)
                current=self._get("settings","main") or {}
                restored["settings_revision"]=max(int(restored.get("settings_revision",0)),int(current.get("settings_revision",0)))+1
                payload=self._seal(restored)
            self._query("INSERT INTO records(collection,record_id,payload,updated_at) VALUES(?,?,?,?) ON CONFLICT(collection,record_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",(collection,record_id,payload,str(item["updated_at"])))
        elif kind=="r2":
            key=str(item["key"])
            if not key.startswith(f"{self.root}/"): raise ValueError("Backup contains an object outside this control ID.")
            args={"Bucket":self.bucket,"Key":key,"Body":base64.b64decode(item["data"],validate=True)}
            if item.get("content_type"):args["ContentType"]=item["content_type"]
            if item.get("cache_control"):args["CacheControl"]=item["cache_control"]
            if item.get("metadata"):args["Metadata"]={str(k):str(v) for k,v in item["metadata"].items()}
            self.s3.put_object(**args)
        else: raise ValueError("Unknown backup item type.")

    def _ensure_schema(self):
        if not self._schema_ready:
            self._query("CREATE TABLE IF NOT EXISTS records (collection TEXT NOT NULL, record_id TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (collection, record_id));")
            # D1 does not offer a read/modify/write transaction across separate
            # HTTP requests. This lease table serializes those callbacks across
            # the desktop GUI and Railway, which share the same database.
            self._query("CREATE TABLE IF NOT EXISTS operation_locks (lock_key TEXT PRIMARY KEY, expires_at REAL NOT NULL);")
            self._schema_ready = True
            # Retire the removed monitor and voice-session collections, including saved history.
            self._query("DELETE FROM records WHERE collection IN (?,?)", ("member_activity", "member_voice_sessions"))
            settings=self._get("settings","main")
            retired_settings = {"monitored_users", "discipline", "token"}
            if settings and retired_settings.intersection(settings):
                for retired_key in retired_settings:
                    settings.pop(retired_key, None)
                settings["settings_revision"]=int(settings.get("settings_revision",0))+1
                self._put("settings","main",settings)

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
        self._ensure_schema()
        key = f"{collection}:{ident}"
        local_lock = self._local_locks.setdefault(key, threading.Lock())
        # The local lock reduces D1 polling for calls made by this process; the
        # conditional UPSERT is the actual cross-process lock.
        with local_lock:
            deadline = time.monotonic() + 30
            acquired = False
            while time.monotonic() < deadline:
                now = time.time()
                result = self._query(
                    "INSERT INTO operation_locks(lock_key,expires_at) VALUES(?,?) "
                    "ON CONFLICT(lock_key) DO UPDATE SET expires_at=excluded.expires_at "
                    "WHERE operation_locks.expires_at < ?",
                    (key, now + 120, now),
                )
                meta = result.get("meta", {})
                if int(meta.get("changes", 0)) > 0:
                    acquired = True
                    break
                time.sleep(0.1)
            if not acquired:
                raise RuntimeError("Cloudflare D1 record is busy; please retry.")
            try:
                current = self._get(collection, ident)
                return fn(current if current is not None else default)
            finally:
                self._query("DELETE FROM operation_locks WHERE lock_key=?", (key,))
