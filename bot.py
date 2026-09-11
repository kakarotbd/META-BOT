"""Desktop GUI control panel. Settings, warnings, and logs are shared through encrypted Firebase."""
import asyncio, queue, threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from dotenv import load_dotenv
from bot_core import ModeratorBot
from firebase_store import EncryptedFirestore
load_dotenv()

class App:
    def __init__(self, root):
        self.root, self.q, self.thread = root, queue.Queue(), None
        root.title("Discord Moderator — Firebase Control Panel"); root.geometry("820x620")
        self.store = EncryptedFirestore(); self.settings = self.store.get_settings(); self.guilds, self.info, self.runtime, self.tickets = [], {}, None, {}
        tabs = ttk.Notebook(root); tabs.pack(fill="both", expand=True, padx=10, pady=10)
        self.connection, self.permissions, self.tickets_tab, self.moderation_tab, self.log_tab = tk.Frame(tabs), tk.Frame(tabs), tk.Frame(tabs), tk.Frame(tabs), tk.Frame(tabs)
        tabs.add(self.connection, text="Connection"); tabs.add(self.permissions, text="Link Permissions"); tabs.add(self.tickets_tab, text="Support Tokens"); tabs.add(self.moderation_tab, text="Kick / Ban"); tabs.add(self.log_tab, text="Live Logs")
        self.connection_ui(); self.permissions_ui(); self.tickets_ui(); self.moderation_ui()
        tk.Button(self.log_tab,text="Load shared Firebase logs",command=self.load_logs).pack(pady=(8,0))
        self.logs = scrolledtext.ScrolledText(self.log_tab, state="disabled", height=30); self.logs.pack(fill="both", expand=True, padx=10, pady=10)
        root.after(200, self.poll)
        if self.settings.get("auto_connect") and self.settings.get("token"):
            root.after(500, self.connect)

    def connection_ui(self):
        f = self.connection; tk.Label(f, text="Discord token (encrypted before Firebase save)").pack(pady=(25,4))
        self.token = tk.Entry(f, width=70, show="*"); self.token.pack(); self.token.insert(0, self.settings.get("token", ""))
        self.auto = tk.BooleanVar(value=self.settings.get("auto_connect", True)); tk.Checkbutton(f, text="Auto-connect this GUI", variable=self.auto).pack(pady=8)
        limits=tk.Frame(f); limits.pack(pady=4)
        tk.Label(limits,text="Warnings before timeout:").grid(row=0,column=0,padx=5)
        self.warning_limit=tk.Spinbox(limits,from_=1,to=50,width=6); self.warning_limit.delete(0,tk.END); self.warning_limit.insert(0,self.settings.get("max_warnings_before_timeout",5)); self.warning_limit.grid(row=0,column=1,padx=5)
        tk.Label(limits,text="Timeout minutes:").grid(row=0,column=2,padx=5)
        self.timeout_minutes=tk.Spinbox(limits,from_=1,to=10080,width=8); self.timeout_minutes.delete(0,tk.END); self.timeout_minutes.insert(0,self.settings.get("timeout_minutes",10)); self.timeout_minutes.grid(row=0,column=3,padx=5)
        row = tk.Frame(f); row.pack(pady=8)
        tk.Button(row,text="Save to Firebase",command=self.save_connection,width=20).grid(row=0,column=0,padx=5)
        tk.Button(row,text="Connect GUI bot",command=self.connect,width=20).grid(row=0,column=1,padx=5)
        self.status=tk.Label(f,text="Status: Disconnected"); self.status.pack(pady=10)
        tk.Label(f,text="Railway reads the same encrypted settings automatically within 10 seconds.",wraplength=650).pack(pady=15)

    def permissions_ui(self):
        f=self.permissions; tk.Label(f,text="Server").pack(pady=(18,3))
        self.server=ttk.Combobox(f,state="readonly",width=55); self.server.pack(); self.server.bind("<<ComboboxSelected>>",lambda e:self.show_roles())
        tk.Label(f,text="Allowed roles — select one or more").pack(pady=(18,3))
        self.roles=tk.Listbox(f,selectmode="multiple",width=65,height=16); self.roles.pack()
        tk.Button(f,text="Save permissions to Firebase",command=self.save_roles,width=32).pack(pady=15)

    def tickets_ui(self):
        f=self.tickets_tab; top=tk.Frame(f); top.pack(fill="x",pady=10)
        tk.Button(top,text="Refresh encrypted tickets",command=self.load_tickets).pack(side="left",padx=10)
        tk.Label(top,text="Select a ticket, add a resolution note, then mark it resolved.").pack(side="left")
        columns=("id","status","user","category","contact","problem")
        self.ticket_tree=ttk.Treeview(f,columns=columns,show="headings",height=14)
        for name,width in zip(columns,(75,75,120,100,160,260)):
            self.ticket_tree.heading(name,text=name.title()); self.ticket_tree.column(name,width=width,anchor="w")
        self.ticket_tree.pack(fill="both",expand=True,padx=10)
        tk.Label(f,text="Resolution note").pack(anchor="w",padx=10,pady=(8,2))
        self.resolution=tk.Entry(f,width=100); self.resolution.pack(padx=10)
        tk.Button(f,text="Mark selected ticket resolved + DM user",command=self.resolve_ticket,width=42).pack(pady=10)

    def moderation_ui(self):
        f=self.moderation_tab
        tk.Label(f,text="Connect the GUI bot first. Enter the Discord member ID and a reason.").pack(pady=(25,8))
        tk.Label(f,text="Server").pack(); self.mod_guild=ttk.Combobox(f,state="readonly",width=55); self.mod_guild.pack()
        tk.Label(f,text="Member ID").pack(pady=(12,2)); self.member_id=tk.Entry(f,width=45); self.member_id.pack()
        tk.Label(f,text="Reason").pack(pady=(12,2)); self.mod_reason=tk.Entry(f,width=70); self.mod_reason.pack()
        row=tk.Frame(f); row.pack(pady=18)
        tk.Button(row,text="Kick member",command=lambda:self.moderate("kick"),width=20).grid(row=0,column=0,padx=8)
        tk.Button(row,text="Ban member",command=lambda:self.moderate("ban"),width=20).grid(row=0,column=1,padx=8)

    def log(self, message):
        self.q.put(("log",message))
        try: self.store.add_log("gui",message)
        except Exception: pass

    def load_logs(self):
        try:
            records=self.store.recent_logs()
            self.logs.config(state="normal"); self.logs.delete("1.0",tk.END)
            for record in records:self.logs.insert(tk.END,f"[{record['at']}] {record['source']}: {record['message']}\n")
            self.logs.config(state="disabled")
        except Exception as exc: messagebox.showerror("Log error",str(exc))

    def save_connection(self):
        token=self.token.get().strip()
        if not token: messagebox.showwarning("Token missing","Enter a bot token."); return False
        try:
            warning_limit,timeout_minutes=int(self.warning_limit.get()),int(self.timeout_minutes.get())
            if warning_limit < 1 or timeout_minutes < 1: raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid limits","Use positive whole numbers for warnings and timeout."); return False
        self.settings["token"],self.settings["auto_connect"]=token,self.auto.get()
        self.settings["max_warnings_before_timeout"],self.settings["timeout_minutes"]=warning_limit,timeout_minutes
        self.settings=self.store.save_settings(self.settings)
        messagebox.showinfo("Saved","Encrypted settings saved. Railway synchronizes within 10 seconds."); return True

    def connect(self):
        if self.thread or not self.save_connection(): return
        def run():
            try:
                self.runtime=ModeratorBot(self.store,"gui",self.log,lambda kind,data:self.q.put((kind,data)))
                asyncio.run(self.runtime.run(self.settings["token"]))
            except Exception as exc: self.q.put(("log",f"Connection error: {exc}"))
            finally: self.thread=None; self.runtime=None; self.q.put(("status","Disconnected"))
        self.thread=threading.Thread(target=run,daemon=True); self.thread.start(); self.status.config(text="Status: Connecting")

    def show_roles(self):
        i=self.server.current()
        if i<0:return
        gid=self.guilds[i][0]; data=self.info.get(gid,{}) ; allowed=set(self.settings.get("allowed_roles",{}).get(str(gid),[])); self.roles.delete(0,tk.END)
        for n,role in enumerate(data.get("roles",[])):
            self.roles.insert(tk.END,role["name"])
            if role["id"] in allowed:self.roles.selection_set(n)

    def save_roles(self):
        i=self.server.current()
        if i<0: return messagebox.showwarning("Server missing","Connect the GUI bot and select a server first.")
        gid=self.guilds[i][0]; roles=self.info[gid]["roles"]
        self.settings.setdefault("allowed_roles",{})[str(gid)]=[roles[i]["id"] for i in self.roles.curselection()]; self.settings=self.store.save_settings(self.settings)
        messagebox.showinfo("Saved","Permissions encrypted and saved. Railway synchronizes within 10 seconds.")

    def load_tickets(self):
        try:
            self.tickets={ticket["document_id"]:ticket for ticket in self.store.list_tickets()}
            for item in self.ticket_tree.get_children(): self.ticket_tree.delete(item)
            for doc,ticket in self.tickets.items():
                category=ticket["category"] + (f" ({ticket['other_category']})" if ticket.get("other_category") else "")
                self.ticket_tree.insert("","end",iid=doc,values=(ticket["ticket_id"],ticket["status"],ticket["username"],category,ticket["contact"],ticket["problem"][:80]))
        except Exception as exc: messagebox.showerror("Ticket error",str(exc))

    def submit_on_bot_loop(self, coroutine, success):
        if not self.runtime or not self.runtime.loop:
            return messagebox.showwarning("GUI bot offline","Connect the GUI bot first so Discord can send the action.")
        future=asyncio.run_coroutine_threadsafe(coroutine,self.runtime.loop)
        def done(task):
            try: task.result(); self.q.put(("log",success)); self.q.put(("refresh_tickets",None))
            except Exception as exc: self.q.put(("log",f"Action failed: {exc}"))
        future.add_done_callback(done)

    def resolve_ticket(self):
        selected=self.ticket_tree.selection()
        if not selected: return messagebox.showwarning("Ticket missing","Select a ticket first.")
        if not self.runtime or not self.runtime.loop: return messagebox.showwarning("GUI bot offline","Connect the GUI bot first to resolve and notify the user.")
        ticket=self.tickets[selected[0]]
        if ticket["status"] == "resolved": return messagebox.showinfo("Already resolved","This ticket is already resolved.")
        try: ticket=self.store.resolve_ticket(selected[0],"GUI operator",self.resolution.get().strip())
        except Exception as exc: return messagebox.showerror("Resolution error",str(exc))
        self.submit_on_bot_loop(self.runtime.notify_ticket_resolved(ticket),f"Resolved ticket #{ticket['ticket_id']} and sent user confirmation.")
        self.load_tickets()

    def moderate(self, action):
        i=self.mod_guild.current()
        if i<0 or not self.member_id.get().strip().isdigit(): return messagebox.showwarning("Missing values","Select a server and enter a numeric member ID.")
        guild_id=self.guilds[i][0]; user_id=self.member_id.get().strip()
        self.submit_on_bot_loop(self.runtime.moderate_member(action,guild_id,user_id,self.mod_reason.get().strip()),f"{action.title()} action completed for {user_id}.")

    def poll(self):
        try:
            while True:
                kind,data=self.q.get_nowait()
                if kind=="log": self.logs.config(state="normal"); self.logs.insert(tk.END,data+"\n"); self.logs.see(tk.END); self.logs.config(state="disabled")
                elif kind=="guilds":
                    self.guilds=data; values=[f"{name} ({gid})" for gid,name in data]; self.server["values"]=values; self.mod_guild["values"]=values
                    if data: self.server.current(0); self.mod_guild.current(0)
                    self.status.config(text="Status: Connected")
                elif kind=="guild": self.info[data["id"]]=data; self.show_roles()
                elif kind=="status": self.status.config(text=f"Status: {data}")
                elif kind=="refresh_tickets": self.load_tickets()
        except queue.Empty: pass
        self.root.after(200,self.poll)

if __name__=="__main__":
    root=tk.Tk(); App(root); root.mainloop()
