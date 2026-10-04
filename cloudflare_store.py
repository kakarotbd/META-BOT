"""Encrypted Cloudflare R2 storage for all bot state.

R2 is an object store, so records are stored as small encrypted JSON objects
`<control-id>/records/<collection>/<id>.json`; media is stored as binary R2
objects.  The public methods intentionally match the previous storage layer so
the GUI and worker share exactly the same data model.
"""
import base64, hashlib, json, os, time, uuid, threading
from datetime import datetime, timezone, timedelta

import boto3
from botocore.exceptions import ClientError
from cryptography.fernet import Fernet, InvalidToken

DEFAULT_SETTINGS = {"token":"", "auto_connect":True, "allowed_roles":{}, "command_allowed_roles":{}, "chat_allowed_roles":{}, "link_allowed_roles":{}, "drag_allowed_roles":{}, "ticket_channels":{}, "ticket_moderators":{}, "temp_voice_lobbies":{}, "welcome_goodbye":{}, "leveling":{}, "role_notifications":{}, "movie_announcements":{}, "default_roles":{}, "default_role_managed_ids":{}, "birthdays":{}, "counting_channels":{}, "monitored_users":{}, "word_blocks":{}, "max_warnings_before_timeout":5, "timeout_minutes":10, "settings_revision":1}

class CloudflareR2Store:
    def __init__(self):
        key = os.getenv("BOT_ENCRYPTION_KEY", "").encode()
        if not key: raise RuntimeError("BOT_ENCRYPTION_KEY is required.")
        try: self.cipher = Fernet(key)
        except ValueError as exc: raise RuntimeError("BOT_ENCRYPTION_KEY must be a valid Fernet key.") from exc
        endpoint = os.getenv("R2_ENDPOINT", "").strip(); access = os.getenv("R2_ACCESS_KEY_ID", "").strip(); secret = os.getenv("R2_SECRET_ACCESS_KEY", "").strip(); bucket = os.getenv("R2_BUCKET_NAME", "").strip()
        if not all((endpoint, access, secret, bucket)): raise RuntimeError("R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME are required.")
        self.s3 = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access, aws_secret_access_key=secret, region_name="auto")
        self.bucket = bucket; self.control_id = os.getenv("BOT_CONTROL_ID", "production").strip()
        self.root = f"controls/{self.control_id}"; self._locks = {}; self._local_locks = {}

    def _seal(self, value): return self.cipher.encrypt(json.dumps(value, separators=(",", ":")).encode()).decode()
    def _open(self, value):
        try: return json.loads(self.cipher.decrypt(value.encode()).decode())
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as exc: raise RuntimeError("Cannot decrypt R2 data. Check BOT_ENCRYPTION_KEY and BOT_CONTROL_ID.") from exc
    def _key(self, collection, ident): return f"{self.root}/records/{collection}/{ident}.json"
    def _put(self, collection, ident, value):
        self.s3.put_object(Bucket=self.bucket, Key=self._key(collection, ident), Body=json.dumps({"payload":self._seal(value), "updated_at":datetime.now(timezone.utc).isoformat()}).encode(), ContentType="application/json")
    def _get(self, collection, ident):
        try: raw=self.s3.get_object(Bucket=self.bucket, Key=self._key(collection, ident))["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"): return None
            raise
        return self._open(json.loads(raw)["payload"])
    def _delete(self, collection, ident): self.s3.delete_object(Bucket=self.bucket, Key=self._key(collection, ident))
    def _list(self, collection):
        out=[]; token=None
        while True:
            args={"Bucket":self.bucket,"Prefix":f"{self.root}/records/{collection}/"}
            if token: args["ContinuationToken"]=token
            page=self.s3.list_objects_v2(**args)
            for item in page.get("Contents", []):
                try: out.append((item["Key"].rsplit("/",1)[-1][:-5], self._open(json.loads(self.s3.get_object(Bucket=self.bucket,Key=item["Key"])["Body"].read())["payload"]), item.get("LastModified")))
                except (KeyError, json.JSONDecodeError): continue
            if not page.get("IsTruncated"): return out
            token=page.get("NextContinuationToken")

    def _atomic(self, collection, ident, fn, default=None):
        """Fast cross-process R2 claim with one conditional lock write and one read."""
        lock=f"{self.root}/locks/{collection}/{ident}.lock"; key=f"{collection}:{ident}"
        local=self._local_locks.setdefault(key, threading.Lock())
        with local:
            for _ in range(30):
                try:
                    self.s3.put_object(Bucket=self.bucket, Key=lock, Body=json.dumps({"expires":time.time()+8}).encode(), IfNoneMatch="*")
                    break
                except ClientError as exc:
                    code=exc.response.get("Error", {}).get("Code", "")
                    if code not in ("PreconditionFailed", "412", "ConditionalRequestConflict"): raise
                    time.sleep(.05)
            else:
                raise RuntimeError("Cloudflare R2 storage is busy; please retry.")
            try:
                current=self._get(collection, ident)
                return fn(current if current is not None else default)
            finally:
                self.s3.delete_object(Bucket=self.bucket, Key=lock)

    @staticmethod
    def _normalize_settings(settings):
        value={**DEFAULT_SETTINGS, **(settings or {})}; legacy=value.get("allowed_roles", {})
        if "command_allowed_roles" not in settings: value["command_allowed_roles"]=dict(legacy)
        if "chat_allowed_roles" not in settings: value["chat_allowed_roles"]=dict(legacy)
        if "link_allowed_roles" not in settings: value["link_allowed_roles"]=dict((settings or {}).get("chat_allowed_roles", legacy))
        return value
    def get_settings(self):
        value=self._get("settings","main")
        if value is None: self._put("settings","main",DEFAULT_SETTINGS); return dict(DEFAULT_SETTINGS)
        return self._normalize_settings(value)
    def save_settings(self, settings):
        value=self._normalize_settings(settings); value["settings_revision"]=int(value.get("settings_revision",0))+1; self._put("settings","main",value); return value

    def _update_settings(self, fn):
        return self._atomic("settings","main",lambda cur: (lambda v: (self._put("settings","main",v),v)[1])(fn(self._normalize_settings(cur or {}))))
    def save_permission_roles(self, name, guild_id, role_ids):
        if name not in {"command_allowed_roles","chat_allowed_roles","link_allowed_roles","drag_allowed_roles"}: raise ValueError("Unknown permission setting.")
        return self._update_settings(lambda v: (v[name].update({str(guild_id):[str(x) for x in role_ids]}), v.__setitem__("settings_revision",int(v.get("settings_revision",0))+1), v)[-1])
    def save_welcome_goodbye_config(self,guild_id,config): return self._update_settings(lambda v:(v["welcome_goodbye"].update({str(guild_id):dict(config)}),v.__setitem__("settings_revision",int(v.get("settings_revision",0))+1),v)[-1])
    def save_ticket_moderators(self,guild_id,user_ids):
        ids=list(dict.fromkeys(str(x) for x in user_ids if str(x).isdigit()))
        def f(v):
            if ids:v["ticket_moderators"][str(guild_id)]=ids
            else:v["ticket_moderators"].pop(str(guild_id),None)
            v["settings_revision"]=int(v.get("settings_revision",0))+1; self._put("ticket_assignment_cursors",str(guild_id),{"next_index":0}); return v
        return self._update_settings(f)
    def save_default_roles(self,guild_id,role_ids):
        ids=[str(x) for x in role_ids]
        def f(v):
            gid=str(guild_id); previous=v["default_roles"].get(gid,[]); hist=set(v["default_role_managed_ids"].get(gid,previous)); hist.update(ids)
            if ids:v["default_roles"][gid]=ids
            else:v["default_roles"].pop(gid,None)
            if hist:v["default_role_managed_ids"][gid]=sorted(hist)
            v["settings_revision"]=int(v.get("settings_revision",0))+1; return v
        return self._update_settings(f)

    def _claim(self, collection, ident, predicate, value):
        return self._atomic(collection,ident,lambda cur: False if predicate(cur or {}) else (self._put(collection,ident,value),True)[1],{})
    def claim_member_greeting(self,guild_id,member_id,kind,window_seconds=60): return self._claim("greeting_event_state",f"{guild_id}_{member_id}_{kind}",lambda x:time.time()-float(x.get("at",0))<window_seconds,{"at":time.time()})
    def claim_rate_limit(self,action,subject_id,seconds): return self._claim("rate_limits",f"{action}_{subject_id}",lambda x:time.time()-float(x.get("last",0))<seconds,{"last":time.time()})
    def claim_temp_voice_creation(self,guild_id,lobby_id,user_id,seconds=30): return self.claim_rate_limit("temp_voice",f"{guild_id}_{lobby_id}_{user_id}",seconds)
    def release_temp_voice_creation(self,guild_id,lobby_id,user_id): self._put("rate_limits",f"temp_voice_{guild_id}_{lobby_id}_{user_id}",{"last":0})
    def claim_external_guild_notice(self,guild_id): return self._claim("external_guild_notices",str(guild_id),lambda x:bool(x),{"notified":True})
    def claim_role_notification_fire(self,entry_id,date_str): return self._claim("role_notification_state",str(entry_id),lambda x:x.get("last_fired_date")==date_str,{"last_fired_date":date_str})
    def claim_movie_announcement_fire(self,entry_id,date_str): return self._claim("movie_announcement_state",str(entry_id),lambda x:x.get("fired_date")==date_str,{"fired_date":date_str})
    def claim_birthday_fire(self,guild_id,user_id,date_str): return self._claim("birthday_fire_state",f"{guild_id}_{user_id}",lambda x:x.get("last_fired_date")==date_str,{"last_fired_date":date_str})

    def increment_warning(self,user_id):
        return self._atomic("warnings",str(user_id),lambda v:(v.update({"count":int(v.get("count",0))+1}),self._put("warnings",str(user_id),v),v["count"])[-1],{"count":0})
    def reset_warning(self,user_id): self._put("warnings",str(user_id),{"count":0})
    def add_log(self,source,message):
        ident=f"{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"; self._put("logs",ident,{"source":source,"message":message,"at":datetime.now(timezone.utc).isoformat()})
    def recent_logs(self,limit=100): return [v for _,v,_ in sorted(self._list("logs"),key=lambda x:x[2] or datetime.min,reverse=True)[:limit]][::-1]
    def delete_logs_older_than(self,days=30):
        cutoff=datetime.now(timezone.utc)-timedelta(days=int(days)); n=0
        for ident,val,modified in self._list("logs"):
            if modified and modified < cutoff:self._delete("logs",ident);n+=1
        return n

    def _simple(self,c,i): return self._get(c,str(i))
    def _save(self,c,i,v): self._put(c,str(i),v); return v
    def record_word_block_violation(self,guild_id,user_id,word,message_id):
        key=f"{guild_id}_{user_id}"
        def f(v):
            v=v or {"count":0,"words":[],"message_ids":[]}; ids=[str(x) for x in v.get("message_ids",[])][-256:]
            if str(message_id) in ids:return {"duplicate":True,"count":int(v.get("count",0))}
            v["count"]=int(v.get("count",0))+1;v["words"]=(v.get("words",[])+[str(word)])[-20:];v["message_ids"]=(ids+[str(message_id)])[-256:];v["updated_at"]=datetime.now(timezone.utc).isoformat();self._put("word_block_violations",key,v);v["duplicate"]=False;return v
        return self._atomic("word_block_violations",key,f,{})
    def record_member_activity(self,guild_id,user_id,username,event_type,details=None,event_id=None):
        value={"guild_id":str(guild_id),"user_id":str(user_id),"username":str(username),"event_type":str(event_type),"details":details or {},"at":datetime.now(timezone.utc).isoformat()}; self._put("member_activity",event_id or f"{guild_id}_{user_id}_{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}",value); return value
    def recent_member_activity(self,guild_id,user_id,limit=200): return [v for _,v,_ in sorted([(i,v,m) for i,v,m in self._list("member_activity") if str(v.get("guild_id"))==str(guild_id) and str(v.get("user_id"))==str(user_id)],key=lambda x:x[1].get("at",""),reverse=True)[:max(1,min(int(limit),500))]]

    def _save_image(self,c,i,content_type,data):
        key=f"{self.root}/media/{c}/{i}"; self.s3.put_object(Bucket=self.bucket,Key=key,Body=data,ContentType=content_type,CacheControl="public, max-age=31536000, immutable"); self._put(c,i,{"backend":"r2","key":key,"content_type":content_type,"size":len(data)}); return i
    def _get_image(self,c,i):
        v=self._get(c,i)
        if not v:return None
        return v["content_type"],self.s3.get_object(Bucket=self.bucket,Key=v["key"])["Body"].read()
    def save_welcome_goodbye_image(self,i,t,d):return self._save_image("welcome_goodbye_images",i,t,d)
    def get_welcome_goodbye_image(self,i):return self._get_image("welcome_goodbye_images",i)
    def save_notification_image(self,i,t,d):return self._save_image("notification_images",i,t,d)
    def get_notification_image(self,i):return self._get_image("notification_images",i)
    def save_birthday_image(self,i,t,d):return self._save_image("birthday_images",i,t,d)
    def get_birthday_image(self,i):return self._get_image("birthday_images",i)
    def start_member_voice_session(self,guild_id,user_id,username,channel_name):
        key=f"{guild_id}_{user_id}"; value={"guild_id":str(guild_id),"user_id":str(user_id),"username":str(username),"channel_name":str(channel_name),"started_at":datetime.now(timezone.utc).isoformat()}
        return self._atomic("member_voice_sessions",key,lambda old: False if old else (self._put("member_voice_sessions",key,value),True)[1],None)
    def end_member_voice_session(self,guild_id,user_id):
        key=f"{guild_id}_{user_id}"; old=self._get("member_voice_sessions",key)
        if old:self._delete("member_voice_sessions",key)
        return old
    def submit_counting_number(self,guild_id,channel_id,user_id,message_id,submitted_number):
        key=str(guild_id)
        def f(state):
            state=state or {"channel_id":str(channel_id),"rule_version":3,"expected":1,"last_user_id":"","processed_message_ids":[]}
            if state.get("channel_id")!=str(channel_id): state={"channel_id":str(channel_id),"rule_version":3,"expected":1,"last_user_id":"","processed_message_ids":[]}
            ids=[str(x) for x in state.get("processed_message_ids",[])]
            if str(message_id) in ids:return {"duplicate":True}
            expected=int(state.get("expected",1)); correct=int(submitted_number)==expected; next_expected=expected+1 if correct else expected
            state.update({"expected":next_expected,"last_user_id":str(user_id) if correct else "","last_correct_number":int(submitted_number) if correct else 0,"processed_message_ids":(ids+[str(message_id)])[-256:]}); self._put("counting_state",key,state)
            return {"duplicate":False,"correct":correct,"expected":expected,"next_expected":next_expected}
        return self._atomic("counting_state",key,f,None)
    def reset_counting_state(self,guild_id,channel_id): self._put("counting_state",str(guild_id),{"channel_id":str(channel_id),"rule_version":3,"expected":1,"last_user_id":"","last_correct_number":0,"processed_message_ids":[]})
    def save_temp_voice_room(self,room):
        room=dict(room); room["updated_at"]=datetime.now(timezone.utc).isoformat(); self._put("temp_voice_rooms",f"{room['guild_id']}_{room['channel_id']}",room); return room
    def get_temp_voice_room(self,guild_id,channel_id): return self._get("temp_voice_rooms",f"{guild_id}_{channel_id}")
    def list_active_temp_voice_rooms(self,limit=500): return [v for _,v,_ in self._list("temp_voice_rooms") if v.get("status")=="active"][:limit]
    def close_temp_voice_room(self,guild_id,channel_id):
        key=f"{guild_id}_{channel_id}"; room=self._get("temp_voice_rooms",key)
        if room: room.update({"status":"closed","closed_at":datetime.now(timezone.utc).isoformat()}); self._put("temp_voice_rooms",key,room)
    def create_ticket(self,guild_id,user_id,username,category,other_category,contact,problem):
        settings=self.get_settings(); mods=list(dict.fromkeys(map(str,settings.get("ticket_moderators",{}).get(str(guild_id),[]))))
        # Use the configured staff; ADMIN_USER_ID keeps tickets usable before GUI staff setup.
        if not mods:
            fallback=os.getenv("ADMIN_USER_ID","").strip()
            if fallback.isdigit(): mods=[fallback]
        if not mods: raise RuntimeError("No ticket moderators are configured and ADMIN_USER_ID is invalid.")
        key=uuid.uuid4().hex; cursor=self._get("ticket_assignment_cursors",str(guild_id)) or {"next_index":0}; index=int(cursor.get("next_index",0))%len(mods)
        ticket={"ticket_id":key[:8].upper(),"guild_id":str(guild_id),"user_id":str(user_id),"username":username,"category":category,"other_category":other_category,"contact":contact,"problem":problem,"assigned_moderator_id":mods[index],"status":"open","created_at":datetime.now(timezone.utc).isoformat()}; self._put("tickets",key,ticket); self._put("ticket_assignment_cursors",str(guild_id),{"next_index":(index+1)%len(mods)}); ticket["document_id"]=key; return ticket
    def list_tickets(self,limit=200): return [dict(v,document_id=i) for i,v,_ in sorted(self._list("tickets"),key=lambda x:x[1].get("created_at",""),reverse=True)[:limit]]
    def get_ticket(self,document_id):
        v=self._get("tickets",document_id); return dict(v,document_id=document_id) if v else None
    def update_ticket_fields(self,document_id,updates):
        v=self._get("tickets",document_id)
        if not v: raise RuntimeError("Ticket no longer exists.")
        v.update(updates); self._put("tickets",document_id,v); return dict(v,document_id=document_id)
    def set_ticket_status(self,document_id,status,resolver,note):
        v=self._get("tickets",document_id)
        if not v: raise RuntimeError("Ticket no longer exists.")
        if v.get("status")!="open": raise RuntimeError(f"This ticket is already {v.get('status')}.")
        v.update({"status":status,"resolved_by":resolver,"resolution_note":note,"resolved_at":datetime.now(timezone.utc).isoformat()}); self._put("tickets",document_id,v); return dict(v,document_id=document_id)
    def resolve_ticket(self,document_id,resolver,note): return self.set_ticket_status(document_id,"resolved",resolver,note)
    def delete_ticket(self,document_id):
        if not self._get("tickets",document_id): raise RuntimeError("Ticket no longer exists.")
        self._delete("tickets",document_id)
    def cancel_ticket(self,document_id,user_id):
        v=self._get("tickets",document_id)
        if not v: raise RuntimeError("Ticket no longer exists.")
        if str(v["user_id"])!=str(user_id): raise RuntimeError("Only the ticket owner can cancel this ticket.")
        if v.get("status")!="open": raise RuntimeError(f"This ticket is already {v.get('status')}.")
        v.update({"status":"cancelled","cancelled_at":datetime.now(timezone.utc).isoformat()}); self._put("tickets",document_id,v); return dict(v,document_id=document_id)
    def save_ticket_dashboard(self,guild_id,channel_id,message_id): return self._save("ticket_dashboards",guild_id,{"channel_id":str(channel_id),"message_id":str(message_id)})
    def get_ticket_dashboard(self,guild_id): return self._simple("ticket_dashboards",guild_id)
    def save_birthday(self,guild_id,user_id,username,day,month,year): return self._save("birthdays",f"{guild_id}_{user_id}",{"guild_id":str(guild_id),"user_id":str(user_id),"username":username,"day":int(day),"month":int(month),"year":int(year) if year else None})
    def get_birthday(self,guild_id,user_id): return self._simple("birthdays",f"{guild_id}_{user_id}")
    def list_birthdays(self,guild_id): return [v for _,v,_ in self._list("birthdays") if str(v.get("guild_id"))==str(guild_id)]
    def save_birthday_dashboard(self,guild_id,channel_id,message_id): return self._save("birthday_dashboards",guild_id,{"channel_id":str(channel_id),"message_id":str(message_id)})
    def get_birthday_dashboard(self,guild_id): return self._simple("birthday_dashboards",guild_id)
    def award_activity_xp(self,guild_id,user_id,username,avatar_url,xp,cooldown_seconds,level_base,level_multiplier,source="text"):
        key=f"{guild_id}_{user_id}"; field="last_xp_at" if source=="text" else f"last_{source}_xp_at"
        def f(v):
            v=v or {"guild_id":str(guild_id),"user_id":str(user_id),"level":1,"xp":0,"total_xp":0,"last_xp_at":0,"last_voice_xp_at":0}; now=time.time()
            if now-float(v.get(field,0))<cooldown_seconds:return None
            old=int(v.get("level",1)); v["xp"]=int(v.get("xp",0))+int(xp); v["total_xp"]=int(v.get("total_xp",0))+int(xp); v[field]=now; v["username"]=username; v["avatar_url"]=avatar_url
            while v["xp"]>=int(round(level_base*(int(v["level"])**level_multiplier))): v["xp"]-=int(round(level_base*(int(v["level"])**level_multiplier))); v["level"]+=1
            self._put("level_users",key,v); return {"level":v["level"],"old_level":old,"xp":v["xp"],"leveled_up":v["level"]>old}
        return self._atomic("level_users",key,f,{})

