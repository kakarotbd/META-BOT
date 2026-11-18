"""Desktop GUI control panel. Settings, warnings, and logs are shared through encrypted Cloudflare R2."""

import asyncio, calendar, io, os, queue, threading, uuid, webbrowser, json, copy

from datetime import date, datetime, timedelta, timezone

from tzlocal import get_localzone_name

import tkinter as tk

from tkinter import filedialog, messagebox, scrolledtext, ttk

from PIL import Image, ImageSequence, ImageTk

from runtime.moderator import ModeratorBot, DEFAULT_TICKET_CLOSE_NOTE

from storage.d1_store import CloudflareD1Store

from config.app_config import APP_CONFIG
from features.log import SharedLogger



MAX_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024  # Non-GIF limit only; original GIFs are stored in R2.

MAX_STORED_IMAGE_BYTES = 490_000



class App:

    BG, PANEL, TEXT, MUTED, ACCENT, SUCCESS = "#0b1020", "#141b2d", "#f3f4f6", "#a7b0c3", "#5865f2", "#43b581"



    def configure_theme(self, root):

        root.configure(bg=self.BG)

        for option, value in (("*Font", "{Segoe UI} 10"), ("*Background", self.PANEL), ("*Foreground", self.TEXT), ("*Button.Background", self.ACCENT), ("*Button.Foreground", "#ffffff"), ("*Entry.Background", "#0f172a"), ("*Entry.Foreground", self.TEXT), ("*Entry.insertBackground", self.TEXT), ("*Listbox.Background", "#0f172a"), ("*Listbox.Foreground", self.TEXT), ("*Listbox.selectBackground", self.ACCENT), ("*Text.Background", "#0f172a"), ("*Text.Foreground", self.TEXT)):

            root.option_add(option, value)

        style = ttk.Style(root); style.theme_use("clam")

        style.configure("TFrame", background=self.PANEL); style.configure("TLabel", background=self.PANEL, foreground=self.TEXT)

        style.configure("TButton", background=self.ACCENT, foreground="#ffffff", padding=(12, 7), borderwidth=0); style.map("TButton", background=[("active", "#4752c4")])

        style.configure("TCombobox", fieldbackground="#0f172a", background="#1a2338", foreground=self.TEXT, arrowcolor=self.TEXT, padding=6)

        style.map("TCombobox", fieldbackground=[("readonly", "#0f172a")], foreground=[("readonly", self.TEXT)])

        style.configure("TNotebook", background=self.BG, borderwidth=0, tabmargins=(4, 8, 4, 0)); style.configure("TNotebook.Tab", background=self.PANEL, foreground=self.MUTED, padding=(16, 11), borderwidth=0, font=("Segoe UI", 9, "bold"))

        style.map("TNotebook.Tab", background=[("selected", self.ACCENT)], foreground=[("selected", "#ffffff")])

        style.configure("Treeview", background="#0f172a", fieldbackground="#0f172a", foreground=self.TEXT, rowheight=28, borderwidth=0)

        style.configure("Treeview.Heading", background="#1a2338", foreground=self.TEXT, relief="flat", padding=7); style.map("Treeview", background=[("selected", self.ACCENT)])
        style.configure("TProgressbar", background=self.ACCENT, troughcolor="#202a40", bordercolor="#202a40", lightcolor=self.ACCENT, darkcolor=self.ACCENT)
        style.configure("Vertical.TScrollbar", background="#202a40", troughcolor=self.BG, bordercolor=self.BG, arrowcolor=self.MUTED)



    def make_scrollable_tab(self, tabs):

        page = tk.Frame(tabs, bg=self.PANEL)

        canvas = tk.Canvas(page, bg=self.PANEL, highlightthickness=0, borderwidth=0)

        bar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)

        canvas.configure(yscrollcommand=bar.set)

        bar.pack(side="right", fill="y"); canvas.pack(side="left", fill="both", expand=True)

        content = tk.Frame(canvas, bg=self.PANEL)

        window = canvas.create_window((0, 0), window=content, anchor="nw")

        content.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))

        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))

        self.scroll_regions[content] = canvas

        return page, content



    def scroll_tab(self, event, direction=None):

        widget = event.widget

        if event.state & 0x0001:

            step = direction if direction is not None else (-1 if event.delta > 0 else 1)

            self.navigate_tab(step)

            return "break"

        if isinstance(widget, (ttk.Combobox, tk.Listbox, tk.Text, ttk.Treeview)):

            return None

        while widget is not None:

            canvas = self.scroll_regions.get(widget)

            if canvas:

                step = direction if direction is not None else -int(event.delta / 120)

                if step: canvas.yview_scroll(step, "units")

                return "break"

            widget = getattr(widget, "master", None)

        return None



    def navigate_tab(self, direction):

        if not getattr(self, "tabs", None): return

        count = len(self.tabs.tabs())

        if count: self.tabs.select((self.tabs.index(self.tabs.select()) + direction) % count)



    def scroll_tabs(self, event, direction=None):

        self.navigate_tab(direction if direction is not None else (-1 if event.delta > 0 else 1))

        return "break"



    def lock_combobox_wheel(self):

        def lock(widget):

            for child in widget.winfo_children():

                if isinstance(child, ttk.Combobox):

                    child.bind("<MouseWheel>", lambda event: "break")

                    child.bind("<Button-4>", lambda event: "break")

                    child.bind("<Button-5>", lambda event: "break")

                lock(child)

        lock(self.root)



    def animate_selection(self, event=None):

        self.header_subtitle.config(text="Selection updated", fg="#8ea1ff")

        if getattr(self, "selection_animation", None): self.root.after_cancel(self.selection_animation)

        self.selection_animation = self.root.after(900, lambda: self.header_subtitle.config(text=APP_CONFIG.bot_app_tagline, fg=self.MUTED))

    def animate_tab_change(self, event=None):
        if not self.tabs.tabs():
            return
        label = self.tabs.tab(self.tabs.select(), "text")
        if getattr(self, "tab_animation", None):
            self.root.after_cancel(self.tab_animation)
        shades = ["#9aaaff", "#aebaff", "#c3cbff", "#d9ddff", self.MUTED]
        def step(index=0):
            if index >= len(shades):
                self.header_subtitle.config(text=APP_CONFIG.bot_app_tagline, fg=self.MUTED)

                return
            self.header_subtitle.config(text=label, fg=shades[index])
            self.tab_animation = self.root.after(45, lambda: step(index + 1))
        step()

    def animate_navigation_indicator(self):
        """Briefly sweep the header accent under the active section."""
        if getattr(self, "indicator_animation", None):
            self.root.after_cancel(self.indicator_animation)
        self.nav_indicator.place(relx=0, rely=1, anchor="sw", relheight=0.045, relwidth=0.015)

        def step(frame=1):
            progress = min(1.0, 0.015 + frame * 0.11)
            self.nav_indicator.place_configure(relwidth=progress)
            if progress < 1.0:
                self.indicator_animation = self.root.after(16, lambda: step(frame + 1))
            else:
                self.indicator_animation = self.root.after(100, self.nav_indicator.place_forget)

        step()

    def polish_buttons(self, parent=None):
        """Add consistent pointer and subtle hover feedback to classic Tk buttons."""
        parent = parent or self.root
        for widget in parent.winfo_children():
            if isinstance(widget, tk.Button):
                base = widget.cget("bg")
                hover = base
                try:
                    channels = [int(base[index:index + 2], 16) for index in (1, 3, 5)]
                    hover = "#" + "".join(f"{min(255, channel + 12):02x}" for channel in channels)
                except (ValueError, TypeError):
                    pass
                widget.configure(cursor="hand2", activebackground=hover, relief="flat", bd=0, highlightthickness=0)
                widget.bind("<Enter>", lambda event: (setattr(event.widget, "_hover_restore_bg", event.widget.cget("bg")), event.widget.configure(bg=event.widget.cget("activebackground"))) if str(event.widget.cget("state")) != "disabled" else None, add="+")
                widget.bind("<Leave>", lambda event: event.widget.configure(bg=getattr(event.widget, "_hover_restore_bg", base)) if str(event.widget.cget("state")) != "disabled" else None, add="+")
            self.polish_buttons(widget)
    def open_calendar_picker(self, entry, on_change=None):

        """A small built-in calendar picker that writes YYYY-MM-DD."""

        try: selected = datetime.strptime(entry.get().strip(), "%Y-%m-%d").date()

        except ValueError: selected = date.today()

        state = {"month": selected.replace(day=1)}

        popup = tk.Toplevel(self.root); popup.title("Select date"); popup.configure(bg=self.PANEL); popup.transient(self.root); popup.grab_set(); popup.resizable(False, False)

        title = tk.Label(popup, bg=self.PANEL, fg=self.TEXT, font=("Segoe UI", 11, "bold")); title.grid(row=0, column=1, columnspan=5, pady=(10, 4))

        grid = tk.Frame(popup, bg=self.PANEL); grid.grid(row=2, column=0, columnspan=7, padx=10, pady=(0, 10))

        def render():

            for child in grid.winfo_children(): child.destroy()

            title.config(text=state["month"].strftime("%B %Y"))

            for column, label in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):

                tk.Label(grid, text=label, bg=self.PANEL, fg=self.MUTED, width=4).grid(row=0, column=column, pady=(0, 3))

            for row, week in enumerate(calendar.monthcalendar(state["month"].year, state["month"].month), 1):

                for column, day_number in enumerate(week):

                    if not day_number: continue

                    day_value = state["month"].replace(day=day_number)

                    active = day_value == selected

                    tk.Button(grid, text=str(day_number), width=3, bg=self.ACCENT if active else "#1a2338", command=lambda value=day_value: choose(value)).grid(row=row, column=column, padx=1, pady=1)

        def shift(months):

            current = state["month"]; next_month = current.month + months

            year = current.year + ((next_month - 1) // 12); month = ((next_month - 1) % 12) + 1

            state["month"] = date(year, month, 1); render()

        def choose(value):

            entry.config(state="normal"); entry.delete(0, tk.END); entry.insert(0, value.strftime("%Y-%m-%d")); popup.destroy()

            if on_change: on_change()

        tk.Button(popup, text="<", width=3, command=lambda: shift(-1)).grid(row=0, column=0, padx=(10, 2), pady=(10, 4))
        tk.Button(popup, text=">", width=3, command=lambda: shift(1)).grid(row=0, column=6, padx=(2, 10), pady=(10, 4))
        render()



    def open_clock_picker(self, hour_widget, minute_widget, ampm_widget):

        """12-hour AM/PM clock picker shared by all schedules."""

        popup = tk.Toplevel(self.root); popup.title("Select time"); popup.configure(bg=self.PANEL); popup.transient(self.root); popup.grab_set(); popup.resizable(False, False)

        tk.Label(popup, text=f"Select time ({self.local_timezone})", bg=self.PANEL, fg=self.TEXT, font=("Segoe UI", 11, "bold")).pack(padx=18, pady=(14, 8))

        row = tk.Frame(popup, bg=self.PANEL); row.pack(padx=18, pady=4)

        hour = tk.Spinbox(row, from_=1, to=12, width=4, format="%.0f"); hour.pack(side="left")

        tk.Label(row, text=":", bg=self.PANEL, fg=self.TEXT).pack(side="left")

        minute = tk.Spinbox(row, from_=0, to=59, width=4, format="%02.0f"); minute.pack(side="left")

        ampm = ttk.Combobox(row, state="readonly", values=["AM", "PM"], width=5); ampm.pack(side="left", padx=(7, 0))

        hour.delete(0, tk.END); hour.insert(0, hour_widget.get() or "8"); minute.delete(0, tk.END); minute.insert(0, minute_widget.get() or "0"); ampm.set(ampm_widget.get() or "AM")

        def apply():

            hour_widget.delete(0, tk.END); hour_widget.insert(0, hour.get())

            minute_widget.delete(0, tk.END); minute_widget.insert(0, minute.get())

            ampm_widget.set(ampm.get()); popup.destroy()

        tk.Button(popup, text="Set time", command=apply).pack(pady=(8, 14))

    def build_header(self, root):

        header = tk.Frame(root, bg=self.PANEL, height=54); header.pack(fill="x", padx=14, pady=(12, 0)); header.pack_propagate(False)

        self.header = header

        if APP_CONFIG.logo_path.is_file():

            try:

                logo = Image.open(APP_CONFIG.logo_path); logo.thumbnail((32, 32))

                self.logo_image = ImageTk.PhotoImage(logo.copy())

                tk.Label(header, image=self.logo_image, bg=self.PANEL).pack(side="left", padx=(18, 5))

            except Exception:

                self.logo_image = None

        tk.Label(header, text=APP_CONFIG.bot_app_name, bg=self.PANEL, fg=self.TEXT, font=("Segoe UI", 14, "bold")).pack(side="left", padx=18 if not getattr(self, "logo_image", None) else 0)

        self.header_subtitle = tk.Label(header, text=APP_CONFIG.bot_app_tagline, bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 9))

        self.header_subtitle.pack(side="left", padx=(4, 0))

        self.header_status = tk.Label(header, text="OFFLINE", bg="#2a1830", fg="#ff7b8a", font=("Segoe UI", 9, "bold"), padx=12, pady=6)

        self.header_status.pack(side="right", padx=14, pady=11)

        self.nav_indicator = tk.Frame(header, bg="#8d98ff", height=2)
        self.nav_indicator.place_forget()



    def set_status(self, value):

        online = str(value).lower() == "connected" or "connecting" in str(value).lower()

        self.status.config(text=f"Status: {value}")

        self.header_status.config(text=f"● {str(value).upper()}", bg="#123b31" if online else "#2a1830", fg=self.SUCCESS if online else "#ff7b8a")

    def animate_status(self):

        """Small live pulse for the header connection badge."""

        label = self.header_status

        active = "CONNECTED" in label.cget("text").upper() or "CONNECTING" in label.cget("text").upper()

        if active:

            self.status_pulse = not getattr(self, "status_pulse", False)

            label.config(bg="#164a3b" if self.status_pulse else "#123b31")

        self.root.after(650, self.animate_status)

    def __init__(self, root):

        self.root, self.q, self.thread = root, queue.Queue(), None

        self.local_timezone = "Asia/Dhaka"  # GUI and hosted worker always use Bangladesh time.

        self.configure_theme(root); root.title(f"{APP_CONFIG.bot_app_name} Control Center"); root.geometry("1240x820"); root.minsize(1080, 700); self.build_header(root)

        self.store = CloudflareD1Store(); self.settings = self.store.get_settings(); self.bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip(); self.settings_baseline = copy.deepcopy(self.settings); self.guilds, self.info, self.runtime, self.tickets, self.invite_url = [], {}, None, {}, ""
        self.shared_logger = SharedLogger(
            self.store, "gui", on_message=lambda text: self.q.put(("log", text))
        )

        self.scroll_regions = {}; self.settings_sync_inflight = False

        shell = tk.Frame(root, bg=self.BG); shell.pack(fill="both", expand=True, padx=14, pady=14)

        tabs = ttk.Notebook(shell); tabs.pack(fill="both", expand=True); self.tabs = tabs

        self.connection_page, self.connection = self.make_scrollable_tab(tabs)

        self.custom_commands_page, self.custom_commands_tab = self.make_scrollable_tab(tabs)

        self.chat_permissions_page, self.chat_permissions = self.make_scrollable_tab(tabs)

        self.link_permissions_page, self.link_permissions = self.make_scrollable_tab(tabs)

        self.drag_permissions_page, self.drag_permissions = self.make_scrollable_tab(tabs)

        self.tickets_page, self.tickets_tab = self.make_scrollable_tab(tabs)

        self.voice_page, self.voice_tab = self.make_scrollable_tab(tabs)

        self.greetings_page, self.greetings_tab = self.make_scrollable_tab(tabs)

        self.leveling_page, self.leveling_tab = self.make_scrollable_tab(tabs)

        self.counting_page, self.counting_tab = self.make_scrollable_tab(tabs)

        self.birthday_page, self.birthday_tab = self.make_scrollable_tab(tabs)

        self.notifications_page, self.notifications_tab = self.make_scrollable_tab(tabs)

        self.movie_page, self.movie_tab = self.make_scrollable_tab(tabs)

        self.default_roles_page, self.default_roles_tab = self.make_scrollable_tab(tabs)

        self.moderation_page, self.moderation_tab = self.make_scrollable_tab(tabs)

        self.word_block_page, self.word_block_tab = self.make_scrollable_tab(tabs)

        self.log_page, self.log_tab = self.make_scrollable_tab(tabs)

        tabs.add(self.connection_page, text="Overview"); tabs.add(self.custom_commands_page, text="GIF Commands"); tabs.add(self.chat_permissions_page, text="Chat"); tabs.add(self.link_permissions_page, text="Link Send"); tabs.add(self.drag_permissions_page, text="Drag Permissions"); tabs.add(self.tickets_page, text="Tickets"); tabs.add(self.voice_page, text="Voice"); tabs.add(self.greetings_page, text="Greetings"); tabs.add(self.leveling_page, text="Leveling"); tabs.add(self.counting_page, text="Counting"); tabs.add(self.birthday_page, text="Birthdays"); tabs.add(self.notifications_page, text="Notifications"); tabs.add(self.movie_page, text="Movies"); tabs.add(self.default_roles_page, text="Auto Roles"); tabs.add(self.moderation_page, text="Moderation"); tabs.add(self.word_block_page, text="Word Block"); tabs.add(self.log_page, text="Live Logs")

        self.build_header_controls(tabs)

        self.notif_entries, self.notif_edit_index = {}, None

        self.connection_ui(); self.custom_commands_ui(); self.chat_permissions_ui(); self.link_permissions_ui(); self.drag_permissions_ui(); self.tickets_ui(); self.voice_ui(); self.greetings_ui(); self.leveling_ui(); self.counting_ui(); self.birthday_ui(); self.notifications_ui(); self.movie_ui(); self.default_roles_ui(); self.moderation_ui(); self.word_block_ui()

        self.polish_buttons()

        log_actions = tk.Frame(self.log_tab); log_actions.pack(pady=(8, 0))

        tk.Button(log_actions,text="Load shared Cloudflare R2 logs",command=self.load_logs).pack(side="left", padx=5)

        tk.Button(log_actions, text="Clear logs older than 30 days", command=self.clear_logs_older_than_30_days).pack(side="left", padx=5)

        db_actions = tk.Frame(self.log_tab); db_actions.pack(pady=(8, 0))

        tk.Button(db_actions,text="Database usage",command=self.database_usage).pack(side="left", padx=5)

        tk.Button(db_actions,text="Export all data to JSON",command=self.export_database).pack(side="left", padx=5)

        tk.Button(db_actions,text="Import JSON (resume supported)",command=self.import_database).pack(side="left", padx=5)

        self.database_status=tk.Label(self.log_tab,text="D1 database and R2 media usage appear here.",fg=self.MUTED); self.database_status.pack(pady=5)

        self.database_progress=ttk.Progressbar(self.log_tab,orient="horizontal",mode="determinate",maximum=100,length=520)

        self.database_progress.pack(pady=(0,8))

        self.logs = scrolledtext.ScrolledText(self.log_tab, state="disabled", height=30); self.logs.pack(fill="both", expand=True, padx=10, pady=10)

        tabs.bind("<<NotebookTabChanged>>", lambda event: (self.animate_tab_change(event), self.animate_navigation_indicator()), add="+")

        root.bind_all("<MouseWheel>", self.scroll_tab, add="+"); root.bind_all("<Shift-MouseWheel>", self.scroll_tabs, add="+"); root.bind_all("<Button-4>", lambda event: self.scroll_tab(event, -1), add="+"); root.bind_all("<Button-5>", lambda event: self.scroll_tab(event, 1), add="+"); root.bind_all("<<ComboboxSelected>>", self.animate_selection, add="+"); root.after(0, self.lock_combobox_wheel)

        root.after(200, self.poll); root.after(650, self.animate_status); root.after(5000, self.schedule_settings_sync)

        root.after(1000, self.load_tickets)

        root.after(15000, self.auto_refresh_tickets)

        if self.settings.get("auto_connect") and self.bot_token:

            root.after(500, self.connect)



    def build_header_controls(self, tabs):

        menu=tk.Menu(self.header,tearoff=False,bg="#1a2338",fg=self.TEXT,activebackground=self.ACCENT,activeforeground="#ffffff")

        for index in range(tabs.index("end")):

            label=tabs.tab(index,"text")

            menu.add_command(label=label,command=lambda i=index:tabs.select(i))

        menu.add_separator()

        menu.add_command(label="Database usage",command=self.database_usage)

        menu.add_command(label="Export all data to JSON",command=self.export_database)

        menu.add_command(label="Import JSON backup (resume supported)",command=self.import_database)

        def show_menu():

            try:

                menu.tk_popup(control.winfo_rootx(),control.winfo_rooty()+control.winfo_height())

            finally:

                menu.grab_release()

        control=tk.Button(self.header,text="All Controls  ▾",command=show_menu,bg=self.ACCENT,fg="#ffffff",activebackground="#4752c4",activeforeground="#ffffff",relief="flat",padx=12,pady=6,font=("Segoe UI",9,"bold"),cursor="hand2")

        control.pack(side="left",padx=(14,6),pady=9)

        self.header_controls_menu=menu



    def connection_ui(self):

        f = self.connection

        tk.Label(f, text="Discord bot token", font=("Segoe UI", 14, "bold")).pack(pady=(25, 6))

        token_state = "Loaded from local .env" if self.bot_token else "Missing DISCORD_BOT_TOKEN in local .env"

        tk.Label(f, text=token_state, fg=self.SUCCESS if self.bot_token else "#ff7b8a").pack(pady=(0, 12))

        self.auto = tk.BooleanVar(value=self.settings.get("auto_connect", True)); tk.Checkbutton(f, text="Auto-connect this GUI", variable=self.auto).pack(pady=8)

        row = tk.Frame(f); row.pack(pady=8)

        tk.Button(row,text="Save to Cloudflare R2",command=self.save_connection,width=20).grid(row=0,column=0,padx=5)

        tk.Button(row,text="Connect GUI bot",command=self.connect,width=20).grid(row=0,column=1,padx=5)

        self.status=tk.Label(f,text="Status: Disconnected"); self.status.pack(pady=10)

        tk.Label(f,text="Bot invite link (available after connection)").pack(pady=(10,2))

        self.invite_entry=tk.Entry(f,width=85,state="readonly"); self.invite_entry.pack()

        tk.Button(f,text="Open invite link and add bot to server",command=self.open_invite,width=36).pack(pady=6)

        tk.Label(f,text="Changes are securely shared with the hosted worker.",fg=self.MUTED).pack(pady=15)





    def custom_commands_ui(self):

        f = self.custom_commands_tab

        tk.Label(f, text="Create server GIF slash commands", font=("Segoe UI", 14, "bold")).pack(pady=(14, 4))

        tk.Label(f, text="Example: command name slap, text slap, then paste one direct GIF URL per line. Save updates Railway automatically.", wraplength=760, justify="center").pack(pady=(0, 12))

        form = tk.Frame(f, bg=self.PANEL); form.pack(pady=5)

        tk.Label(form, text="Server:").grid(row=0, column=0, sticky="e", padx=6, pady=6)

        self.custom_server = ttk.Combobox(form, state="readonly", width=48); self.custom_server.grid(row=0, column=1, padx=6, pady=6)

        tk.Label(form, text="Command name:").grid(row=1, column=0, sticky="e", padx=6, pady=6)

        self.custom_name = tk.Entry(form, width=52); self.custom_name.grid(row=1, column=1, padx=6, pady=6)

        tk.Label(form, text="GIF links (one per line):").grid(row=2, column=0, sticky="ne", padx=6, pady=6)

        self.custom_gifs = tk.Text(form, width=52, height=9); self.custom_gifs.grid(row=2, column=1, padx=6, pady=6)

        buttons = tk.Frame(f, bg=self.PANEL); buttons.pack(pady=8)

        tk.Button(buttons, text="Save / update command", command=self.save_custom_command, width=25).pack(side="left", padx=5)

        tk.Button(buttons, text="Delete command", command=self.delete_custom_command, width=18).pack(side="left", padx=5)

        self.custom_list = tk.Listbox(f, width=72, height=8); self.custom_list.pack(pady=8)

        tk.Label(f, text="Discord sync can take a few seconds after saving. Target is a native member picker: /slap ? choose member!? Enter.", wraplength=760, justify="center", fg=self.MUTED).pack(pady=8)

        self.custom_server.bind("<<ComboboxSelected>>", lambda e: self.refresh_custom_commands_ui())

        self.custom_list.bind("<<ListboxSelect>>", lambda e: self.load_selected_custom_command())



    def _custom_guild_id(self):

        index = self.custom_server.current()

        return str(self.guilds[index][0]) if 0 <= index < len(self.guilds) else ""



    def refresh_custom_commands_ui(self):

        self.custom_list.delete(0, tk.END)

        commands = self.settings.get("custom_commands", {}).get(self._custom_guild_id(), {})

        for name, config in sorted(commands.items()):

            self.custom_list.insert(tk.END, f"/{name}  ({len(config.get('gifs', []))} GIF link(s))")



    def load_selected_custom_command(self):

        index = self.custom_list.curselection()

        gid = self._custom_guild_id()

        names = sorted(self.settings.get("custom_commands", {}).get(gid, {}))

        if not index or index[0] >= len(names): return

        config = self.settings["custom_commands"][gid][names[index[0]]]

        self.custom_name.delete(0, tk.END); self.custom_name.insert(0, names[index[0]])

        self.custom_gifs.delete("1.0", tk.END); self.custom_gifs.insert("1.0", "\n".join(config.get("gifs", [])))



    def save_custom_command(self):

        gid = self._custom_guild_id()

        name = self.custom_name.get().strip().lower()

        urls = [line.strip() for line in self.custom_gifs.get("1.0", tk.END).splitlines() if line.strip()]

        if not gid: return messagebox.showwarning("Server missing", "Connect the GUI bot and select a server first.")

        if not name or not __import__("re").fullmatch(r"[a-z0-9_-]{1,32}", name): return messagebox.showwarning("Invalid command", "Use only lowercase letters, numbers, hyphen, or underscore (max 32).")

        if not urls or any(not url.startswith(("http://", "https://")) for url in urls): return messagebox.showwarning("GIF links missing", "Add at least one valid http(s) GIF URL per line.")

        self.settings.setdefault("custom_commands", {}).setdefault(gid, {})[name] = {"text": name, "gifs": list(dict.fromkeys(urls)), "description": f"{name} reaction"}

        self.persist_settings()

        if self.runtime: self.runtime.update_settings(self.settings)

        self.refresh_custom_commands_ui()

        messagebox.showinfo("Saved", f"/{name} saved. hosted worker synchronizes within 10 seconds.")



    def delete_custom_command(self):

        gid = self._custom_guild_id(); name = self.custom_name.get().strip().lower()

        commands = self.settings.get("custom_commands", {}).get(gid, {})

        if name not in commands: return messagebox.showwarning("Command missing", "Select or enter an existing command first.")

        del commands[name]

        self.persist_settings()

        if self.runtime: self.runtime.update_settings(self.settings)

        self.refresh_custom_commands_ui()

        messagebox.showinfo("Deleted", f"/{name} removed. hosted worker will remove it on the next sync.")



    def chat_permissions_ui(self):

        f=self.chat_permissions; tk.Label(f,text="Server").pack(pady=(18,3))

        self.chat_server=ttk.Combobox(f,state="readonly",width=55); self.chat_server.pack(); self.chat_server.bind("<<ComboboxSelected>>",lambda e:self.show_chat_roles())

        tk.Label(f,text="Roles allowed to use the /chat message feature",font=("Segoe UI", 12, "bold")).pack(pady=(18,3))

        self.chat_roles=tk.Listbox(f,selectmode="multiple",width=65,height=16,exportselection=False); self.chat_roles.pack(); self.chat_roles.bind("<<ListboxSelect>>", lambda event: self.update_role_summary(self.chat_roles, self.chat_role_summary))

        self.chat_role_summary=tk.Label(f,text="Selected roles: none",fg=self.TEXT,bg="#080c16",relief="solid",bd=1,padx=10,pady=7,wraplength=700,justify="center"); self.chat_role_summary.pack(pady=(6,0))

        tk.Label(f,text="This permission controls /chat only; it never filters or deletes normal messages.",fg=self.MUTED).pack(pady=(12,2))

        tk.Button(f,text="Save chat permissions to Cloudflare R2",command=self.save_chat_roles,width=34).pack(pady=15)



    def link_permissions_ui(self):

        f=self.link_permissions; tk.Label(f,text="Server").pack(pady=(18,3))

        self.link_server=ttk.Combobox(f,state="readonly",width=55); self.link_server.pack(); self.link_server.bind("<<ComboboxSelected>>",lambda e:self.show_link_roles())

        tk.Label(f,text="Roles allowed to send links in server chat",font=("Segoe UI", 12, "bold")).pack(pady=(18,3))

        self.link_roles=tk.Listbox(f,selectmode="multiple",width=65,height=16,exportselection=False); self.link_roles.pack(); self.link_roles.bind("<<ListboxSelect>>", lambda event: self.update_role_summary(self.link_roles, self.link_role_summary))

        self.link_role_summary=tk.Label(f,text="Selected roles: none",fg=self.TEXT,bg="#080c16",relief="solid",bd=1,padx=10,pady=7,wraplength=700,justify="center"); self.link_role_summary.pack(pady=(6,0))

        tk.Label(f,text="Only these roles can post URLs. The bot removes links from other roles.",fg=self.MUTED).pack(pady=(12,2))

        tk.Button(f,text="Save link permissions to Cloudflare R2",command=self.save_link_roles,width=34).pack(pady=15)

    def drag_permissions_ui(self):

        f=self.drag_permissions; tk.Label(f,text="Server").pack(pady=(18,3))

        self.drag_server=ttk.Combobox(f,state="readonly",width=55); self.drag_server.pack(); self.drag_server.bind("<<ComboboxSelected>>",lambda e:self.show_drag_roles())

        tk.Label(f,text="Roles allowed to use the /move voice-member feature",font=("Segoe UI", 12, "bold")).pack(pady=(18,3))

        self.drag_roles=tk.Listbox(f,selectmode="multiple",width=65,height=16,exportselection=False); self.drag_roles.pack(); self.drag_roles.bind("<<ListboxSelect>>", lambda event: self.update_role_summary(self.drag_roles, self.drag_role_summary))

        self.drag_role_summary=tk.Label(f,text="Selected roles: none",fg=self.TEXT,bg="#080c16",relief="solid",bd=1,padx=10,pady=7,wraplength=700,justify="center"); self.drag_role_summary.pack(pady=(6,0))

        tk.Label(f,text="Only selected roles can run /move. The bot also needs Discord's Move Members permission.",fg=self.MUTED).pack(pady=(12,2))

        tk.Button(f,text="Save drag permissions to Cloudflare R2",command=self.save_drag_roles,width=34).pack(pady=15)

    def tickets_ui(self):

        f=self.tickets_tab; top=tk.Frame(f); top.pack(fill="x",pady=10)

        tk.Button(top,text="Refresh encrypted tickets",command=self.load_tickets).pack(side="left",padx=10)

        tk.Label(top,text="Select a ticket, add a resolution note, then mark it resolved.").pack(side="left")

        config=tk.Frame(f); config.pack(fill="x",padx=10,pady=(0,8))

        tk.Label(config,text="Ticket server:").grid(row=0,column=0,padx=4)

        self.ticket_server=ttk.Combobox(config,state="readonly",width=28); self.ticket_server.grid(row=0,column=1,padx=4)

        self.ticket_server.bind("<<ComboboxSelected>>",lambda event:self.refresh_ticket_channels())

        tk.Label(config,text="Ticket dashboard channel:").grid(row=0,column=2,padx=4)

        self.ticket_channel=ttk.Combobox(config,state="readonly",width=28); self.ticket_channel.grid(row=0,column=3,padx=4)

        tk.Button(config,text="Save ticket channel",command=self.save_ticket_channel).grid(row=0,column=4,padx=4)

        self.ticket_moderator_ids, self.ticket_moderator_details = [], {}

        moderators = tk.LabelFrame(f, text="Ticket moderators - delivery order: 1 -> 2 -> 3 -> 1")
        moderators.pack(fill="x", padx=10, pady=(2, 8))

        tk.Label(moderators, text="Member Discord ID:").grid(row=0, column=0, padx=6, pady=4)

        self.ticket_moderator_id = tk.Entry(moderators, width=24); self.ticket_moderator_id.grid(row=0, column=1, padx=6, pady=4)

        tk.Button(moderators, text="Verify ID + add staff", command=self.add_ticket_moderator_by_id).grid(row=0, column=2, padx=6, pady=4)

        self.ticket_moderator_selected = tk.Listbox(moderators, height=6, width=70, exportselection=False); self.ticket_moderator_selected.grid(row=1, column=0, columnspan=3, padx=6, pady=3)

        tk.Button(moderators, text="Remove selected", command=self.remove_ticket_moderator).grid(row=2, column=0, padx=6, pady=(2, 6))

        tk.Button(moderators, text="Save moderator order", command=self.save_ticket_moderators).grid(row=2, column=1, padx=6, pady=(2, 6))

        tk.Label(f,text="The bot posts a [Ticket] Create Ticket button in that channel - users click it, fill in category and reason, then confirm. No /ticket typing needed.",wraplength=760,justify="left").pack(anchor="w",padx=10,pady=(0,6))
        columns=("id","status","user","moderator","category","created")

        self.ticket_tree=ttk.Treeview(f,columns=columns,show="headings",height=12)

        for name,width in zip(columns,(90,85,160,160,160,220)):

            self.ticket_tree.heading(name,text=name.title()); self.ticket_tree.column(name,width=width,anchor="w")

        self.ticket_tree.pack(fill="both",expand=True,padx=10)

        self.ticket_tree.bind("<<TreeviewSelect>>",lambda event:self.show_ticket_detail())

        tk.Label(f,text="Complete selected ticket information").pack(anchor="w",padx=10,pady=(8,2))

        self.ticket_detail=scrolledtext.ScrolledText(f,height=8,width=100,state="disabled",wrap="word")

        self.ticket_detail.pack(fill="x",padx=10)

        tk.Label(f,text="Resolution / closing note (used for both buttons below; leave blank when closing to use the default apology message)").pack(anchor="w",padx=10,pady=(8,2))

        self.resolution=tk.Entry(f,width=100); self.resolution.pack(padx=10)

        actions=tk.Frame(f); actions.pack(pady=10)

        tk.Button(actions,text="Mark selected ticket resolved + DM user",command=self.resolve_ticket,width=38).grid(row=0,column=0,padx=6)

        tk.Button(actions,text="Close selected ticket + DM user",command=self.close_ticket,width=32).grid(row=0,column=1,padx=6)

        tk.Button(actions,text="Delete selected ticket from Cloudflare R2",command=self.delete_ticket,width=34).grid(row=0,column=2,padx=6)



    def moderation_ui(self):

        f=self.moderation_tab

        tk.Label(f,text="Connect the GUI bot first. Enter the Discord member ID and a reason.").pack(pady=(25,8))

        tk.Label(f,text="Server").pack(); self.mod_guild=ttk.Combobox(f,state="readonly",width=55); self.mod_guild.pack()

        tk.Label(f,text="Member ID").pack(pady=(12,2)); self.member_id=tk.Entry(f,width=45); self.member_id.pack()

        tk.Label(f,text="Reason").pack(pady=(12,2)); self.mod_reason=tk.Entry(f,width=70); self.mod_reason.pack()

        row=tk.Frame(f); row.pack(pady=18)

        tk.Button(row,text="Kick member",command=lambda:self.moderate("kick"),width=20).grid(row=0,column=0,padx=8)

        tk.Button(row,text="Ban member",command=lambda:self.moderate("ban"),width=20).grid(row=0,column=1,padx=8)



    def word_block_ui(self):

        f=self.word_block_tab

        tk.Label(f,text="Word Block - 5 strikes = permanent ban",font=("TkDefaultFont",12,"bold")).pack(pady=(18,5))

        tk.Label(f,text="One word or phrase per line. A matched server message is deleted and logged; the fifth blocked message permanently bans that member. This shared Cloudflare R2 list is used by hosted worker and GUI.",wraplength=760,justify="center").pack(pady=(0,12))

        form=tk.Frame(f); form.pack()

        tk.Label(form,text="Server:").grid(row=0,column=0,padx=6,pady=6,sticky="e")

        self.word_block_server=ttk.Combobox(form,state="readonly",width=52); self.word_block_server.grid(row=0,column=1,padx=6,pady=6)

        self.word_block_server.bind("<<ComboboxSelected>>",lambda event:self.refresh_word_blocks())

        self.word_block_text=scrolledtext.ScrolledText(f,width=70,height=16); self.word_block_text.pack(fill="both",expand=True,padx=30,pady=8)

        tk.Button(f,text="Save word block list to Cloudflare R2",command=self.save_word_blocks,width=34).pack(pady=(4,14))



    def voice_ui(self):

        f=self.voice_tab

        tk.Label(f,text="Temporary Voice Lobby",font=("TkDefaultFont", 12, "bold")).pack(pady=(28,8))

        tk.Label(f,text="Choose the voice channel members join to receive their own temporary room.\nThe bot moves them into a room named after them and posts a button control dashboard in that room's voice chat.",justify="center",wraplength=650).pack(pady=(0,20))

        form=tk.Frame(f); form.pack()

        tk.Label(form,text="Server:").grid(row=0,column=0,padx=6,pady=8,sticky="e")

        self.voice_server=ttk.Combobox(form,state="readonly",width=45); self.voice_server.grid(row=0,column=1,padx=6,pady=8)

        self.voice_server.bind("<<ComboboxSelected>>",lambda event:self.refresh_voice_channels())

        tk.Label(form,text="Join-to-create channel:").grid(row=1,column=0,padx=6,pady=8,sticky="e")

        self.voice_channel=ttk.Combobox(form,state="readonly",width=45); self.voice_channel.grid(row=1,column=1,padx=6,pady=8)

        tk.Button(f,text="Save temporary voice lobby to Cloudflare R2",command=self.save_voice_lobby,width=38).pack(pady=18)

        tk.Label(f,text="The bot invite requests Administrator permission so it can create, manage, and move members between voice channels.",wraplength=650,justify="center").pack(pady=8)



    def greetings_ui(self):

        f=self.greetings_tab; self.welcome_image_path, self.goodbye_image_path = None, None

        tk.Label(f,text="Welcome & Goodbye",font=("TkDefaultFont", 12, "bold")).pack(pady=(16,5))

        tk.Label(f,text="The bot sends: Welcome/Goodbye + @username + your custom message, with the image you upload. Animated GIF target: 1920x1080, infinite loop.",wraplength=700,justify="center").pack(pady=(0,12))

        form=tk.Frame(f); form.pack(fill="x",padx=25)

        tk.Label(form,text="Server:").grid(row=0,column=0,padx=5,pady=6,sticky="e")

        self.greeting_server=ttk.Combobox(form,state="readonly",width=48); self.greeting_server.grid(row=0,column=1,columnspan=2,padx=5,pady=6,sticky="w")

        self.greeting_server.bind("<<ComboboxSelected>>",lambda event:self.refresh_greeting_channels())

        tk.Label(form,text="Welcome channel:").grid(row=1,column=0,padx=5,pady=6,sticky="e")

        self.welcome_channel=ttk.Combobox(form,state="readonly",width=48); self.welcome_channel.grid(row=1,column=1,columnspan=2,padx=5,pady=6,sticky="w")

        tk.Label(form,text="Welcome message:").grid(row=2,column=0,padx=5,pady=6,sticky="e")

        self.welcome_message=tk.Entry(form,width=60); self.welcome_message.grid(row=2,column=1,columnspan=2,padx=5,pady=6,sticky="w")

        tk.Button(form,text="Upload welcome image",command=lambda:self.choose_greeting_image("welcome")).grid(row=3,column=0,padx=5,pady=6)

        self.welcome_image_label=tk.Label(form,text="No new image selected (saved image is kept)",anchor="w"); self.welcome_image_label.grid(row=3,column=1,columnspan=2,padx=5,pady=6,sticky="w")

        self.welcome_preview=tk.Label(form,text="No welcome image saved",anchor="w"); self.welcome_preview.grid(row=4,column=1,columnspan=2,padx=5,pady=(0,6),sticky="w")

        ttk.Separator(f,orient="horizontal").pack(fill="x",padx=28,pady=10)

        form2=tk.Frame(f); form2.pack(fill="x",padx=25)

        tk.Label(form2,text="Goodbye channel:").grid(row=0,column=0,padx=5,pady=6,sticky="e")

        self.goodbye_channel=ttk.Combobox(form2,state="readonly",width=48); self.goodbye_channel.grid(row=0,column=1,columnspan=2,padx=5,pady=6,sticky="w")

        tk.Label(form2,text="Goodbye message:").grid(row=1,column=0,padx=5,pady=6,sticky="e")

        self.goodbye_message=tk.Entry(form2,width=60); self.goodbye_message.grid(row=1,column=1,columnspan=2,padx=5,pady=6,sticky="w")

        tk.Button(form2,text="Upload goodbye image",command=lambda:self.choose_greeting_image("goodbye")).grid(row=2,column=0,padx=5,pady=6)

        self.goodbye_image_label=tk.Label(form2,text="No new image selected (saved image is kept)",anchor="w"); self.goodbye_image_label.grid(row=2,column=1,columnspan=2,padx=5,pady=6,sticky="w")

        self.goodbye_preview=tk.Label(form2,text="No goodbye image saved",anchor="w"); self.goodbye_preview.grid(row=3,column=1,columnspan=2,padx=5,pady=(0,6),sticky="w")

        tk.Button(f,text="Save welcome / goodbye to Cloudflare R2",command=self.save_greetings,width=38).pack(pady=18)



    def counting_ui(self):

        f=self.counting_tab

        tk.Label(f,text="Counting Channel",font=("TkDefaultFont",12,"bold")).pack(pady=(20,6))

        tk.Label(f,text="Select one channel. Members must send 1, 2, 3... in order. Correct messages get [ok] Wrong entries do not change the saved count; only correct entries get a [ok]",wraplength=760,justify="center").pack(pady=(0,14))

        form=tk.Frame(f); form.pack()

        tk.Label(form,text="Server:").grid(row=0,column=0,padx=6,pady=7,sticky="e")

        self.counting_server=ttk.Combobox(form,state="readonly",width=48); self.counting_server.grid(row=0,column=1,padx=6,pady=7)

        self.counting_server.bind("<<ComboboxSelected>>",lambda event:self.refresh_counting_channels())

        tk.Label(form,text="Counting channel:").grid(row=1,column=0,padx=6,pady=7,sticky="e")

        self.counting_channel=ttk.Combobox(form,state="readonly",width=48); self.counting_channel.grid(row=1,column=1,padx=6,pady=7)

        tk.Button(f,text="Save Counting Channel",command=self.save_counting_channel,width=32).pack(pady=(10,5))

        tk.Button(f,text="Reset Count + Clear Channel",command=self.reset_counting_channel,width=32).pack(pady=(2,5))

        tk.Label(f,text="The count is stored atomically in Cloudflare R2, so GUI and hosted worker process every Discord message only once.",fg=self.MUTED,wraplength=720,justify="center").pack(pady=8)

    def birthday_ui(self):

        f=self.birthday_tab; self.birthday_image_path=None

        tk.Label(f,text="Birthdays",font=("TkDefaultFont", 12, "bold")).pack(pady=(16,5))

        tk.Label(f,text="Select a channel where members can submit their birthday and a separate channel for the bot's birthday wishes. Members enter day/month/year in a popup and get a private confirmation.",wraplength=720,justify="center").pack(pady=(0,12))

        form=tk.Frame(f); form.pack(fill="x",padx=25)

        tk.Label(form,text="Server:").grid(row=0,column=0,padx=5,pady=6,sticky="e")

        self.birthday_server=ttk.Combobox(form,state="readonly",width=48); self.birthday_server.grid(row=0,column=1,columnspan=2,padx=5,pady=6,sticky="w")

        self.birthday_server.bind("<<ComboboxSelected>>",lambda event:self.refresh_birthday_channels())

        tk.Label(form,text="Birthday entry channel:").grid(row=1,column=0,padx=5,pady=6,sticky="e")

        self.birthday_channel=ttk.Combobox(form,state="readonly",width=48); self.birthday_channel.grid(row=1,column=1,columnspan=2,padx=5,pady=6,sticky="w")

        tk.Label(form,text="Birthday wish channel:").grid(row=2,column=0,padx=5,pady=6,sticky="e")

        self.birthday_wish_channel=ttk.Combobox(form,state="readonly",width=48); self.birthday_wish_channel.grid(row=2,column=1,columnspan=2,padx=5,pady=6,sticky="w")

        tk.Label(form,text="Birthday role(s) to mention:").grid(row=3,column=0,padx=5,pady=6,sticky="ne")

        self.birthday_roles=tk.Listbox(form,selectmode="multiple",height=5,exportselection=False,width=48); self.birthday_roles.grid(row=3,column=1,columnspan=2,padx=5,pady=6,sticky="w"); self.birthday_roles.bind("<<ListboxSelect>>",lambda event:self.update_role_summary(self.birthday_roles,self.birthday_role_summary))

        self.birthday_role_summary=tk.Label(form,text="Selected roles: none",fg=self.TEXT,bg="#080c16",relief="solid",bd=1,padx=10,pady=7,wraplength=500,justify="left"); self.birthday_role_summary.grid(row=3,column=1,columnspan=2,padx=7,pady=(92,0),sticky="sw")

        tk.Button(form,text="Upload birthday image",command=self.choose_birthday_image).grid(row=4,column=0,padx=5,pady=6)

        self.birthday_image_label=tk.Label(form,text="No new image selected (saved image is kept)",anchor="w"); self.birthday_image_label.grid(row=4,column=1,columnspan=2,padx=5,pady=6,sticky="w")

        self.birthday_preview=tk.Label(form,text="No birthday image saved",anchor="w"); self.birthday_preview.grid(row=5,column=1,columnspan=2,padx=5,pady=(0,6),sticky="w")

        tk.Button(f,text="Save birthday settings to Cloudflare R2",command=self.save_birthday_channel,width=38).pack(pady=18)



    def leveling_ui(self):

        f=self.leveling_tab

        tk.Label(f,text="Activity Leveling",font=("TkDefaultFont",12,"bold")).pack(pady=(20,6))

        tk.Label(f,text="Users earn XP for meaningful messages. XP is protected by a cooldown and saved atomically in Cloudflare R2, so the GUI and hosted worker stay in sync.",wraplength=700,justify="center").pack(pady=(0,14))

        form=tk.Frame(f); form.pack()

        tk.Label(form,text="Server:").grid(row=0,column=0,padx=6,pady=7,sticky="e")

        self.level_server=ttk.Combobox(form,state="readonly",width=45); self.level_server.grid(row=0,column=1,columnspan=3,padx=6,pady=7)

        self.level_server.bind("<<ComboboxSelected>>",lambda event:self.refresh_level_channels())

        tk.Label(form,text="Level-up channel:").grid(row=1,column=0,padx=6,pady=7,sticky="e")

        self.level_channel=ttk.Combobox(form,state="readonly",width=45); self.level_channel.grid(row=1,column=1,columnspan=3,padx=6,pady=7)

        self.level_enabled=tk.BooleanVar(value=False); tk.Checkbutton(form,text="Enable leveling",variable=self.level_enabled).grid(row=2,column=1,sticky="w",pady=7)

        tk.Label(form,text="Base XP/message:").grid(row=3,column=0,padx=6,pady=7,sticky="e")

        self.level_xp=tk.Spinbox(form,from_=1,to=100,width=8); self.level_xp.grid(row=3,column=1,sticky="w",padx=6)

        tk.Label(form,text="Cooldown (seconds):").grid(row=3,column=2,padx=6,pady=7,sticky="e")

        self.level_cooldown=tk.Spinbox(form,from_=5,to=3600,width=8); self.level_cooldown.grid(row=3,column=3,sticky="w",padx=6)

        tk.Label(form,text="XP needed for level 2:").grid(row=4,column=0,padx=6,pady=7,sticky="e")

        self.level_base=tk.Spinbox(form,from_=10,to=10000,width=8); self.level_base.grid(row=4,column=1,sticky="w",padx=6)

        tk.Label(form,text="Requirement multiplier:").grid(row=4,column=2,padx=6,pady=7,sticky="e")

        self.level_multiplier=tk.Spinbox(form,from_=1.0,to=3.0,increment=0.05,width=8); self.level_multiplier.grid(row=4,column=3,sticky="w",padx=6)

        for widget,value in ((self.level_xp,15),(self.level_cooldown,60),(self.level_base,100),(self.level_multiplier,1.5)):

            widget.delete(0,tk.END); widget.insert(0,str(value))

        tk.Button(f,text="Save Leveling Settings",command=self.save_leveling,width=32).pack(pady=(8,4))

        tk.Label(f,text="Formula: each level costs base x current-level^multiplier. A small message-length bonus (up to +10 XP) rewards real conversation; messages below 3 characters and cooldown spam earn nothing.",wraplength=700,justify="center").pack(pady=14)

        tk.Label(f,text="Voice Call XP",font=("TkDefaultFont",12,"bold")).pack(pady=(4,6))

        tk.Label(f,text="Members on a voice call earn XP on their own timer. Voice awards are intentionally 3x slower than this interval, so simply idling in VC cannot level up quickly.",wraplength=700,justify="center").pack(pady=(0,10))

        voice_form=tk.Frame(f); voice_form.pack()

        tk.Label(voice_form,text="XP per interval:").grid(row=0,column=0,padx=6,pady=7,sticky="e")

        self.voice_level_xp=tk.Spinbox(voice_form,from_=1,to=100,width=8); self.voice_level_xp.grid(row=0,column=1,sticky="w",padx=6)

        tk.Label(voice_form,text="Interval (seconds):").grid(row=0,column=2,padx=6,pady=7,sticky="e")

        self.voice_level_interval=tk.Spinbox(voice_form,from_=30,to=1800,width=8); self.voice_level_interval.grid(row=0,column=3,sticky="w",padx=6)

        for widget,value in ((self.voice_level_xp,10),(self.voice_level_interval,60)):

            widget.delete(0,tk.END); widget.insert(0,str(value))

        self.voice_level_require_others=tk.BooleanVar(value=True)

        tk.Checkbutton(f,text="Require another member in the call (blocks solo/AFK farming)",variable=self.voice_level_require_others).pack(pady=(4,2))

        self.voice_level_ignore_deafened=tk.BooleanVar(value=True)

        tk.Checkbutton(f,text="Skip self-deafened members (they aren't actually listening)",variable=self.voice_level_ignore_deafened).pack(pady=(0,10))

        tk.Button(f,text="Save leveling to Cloudflare R2",command=self.save_leveling,width=32).pack(pady=6)



    def movie_ui(self):

        f = self.movie_tab; self.movie_image_path = None

        tk.Label(f, text="Movie Announcement", font=("TkDefaultFont", 12, "bold")).pack(pady=(18, 5))

        tk.Label(f, text="Schedule a one-time movie announcement in your local time. At the selected time the bot sends your role mention, movie title, custom text, and optional poster exactly once.", wraplength=720, justify="center").pack(pady=(0, 12))

        form = tk.Frame(f); form.pack(padx=25, pady=4)

        tk.Label(form, text="Server:").grid(row=0, column=0, padx=5, pady=6, sticky="e")

        self.movie_server = ttk.Combobox(form, state="readonly", width=48); self.movie_server.grid(row=0, column=1, columnspan=3, padx=5, pady=6, sticky="w")

        self.movie_server.bind("<<ComboboxSelected>>", lambda event: self.refresh_movie_ui())

        tk.Label(form, text="Announcement channel:").grid(row=1, column=0, padx=5, pady=6, sticky="e")

        self.movie_channel = ttk.Combobox(form, state="readonly", width=48); self.movie_channel.grid(row=1, column=1, columnspan=3, padx=5, pady=6, sticky="w")

        tk.Label(form, text="Role(s) to mention:").grid(row=2, column=0, padx=5, pady=6, sticky="ne")

        self.movie_roles = tk.Listbox(form, selectmode="multiple", height=5, exportselection=False, width=48); self.movie_roles.grid(row=2, column=1, columnspan=3, padx=5, pady=6, sticky="w"); self.movie_roles.bind("<<ListboxSelect>>", lambda event: self.update_role_summary(self.movie_roles, self.movie_role_summary))

        self.movie_role_summary=tk.Label(form,text="Selected roles: none",fg=self.TEXT,bg="#080c16",relief="solid",bd=1,padx=10,pady=7,wraplength=500,justify="left"); self.movie_role_summary.grid(row=2,column=1,columnspan=3,padx=7,pady=(92,0),sticky="sw")

        tk.Label(form, text="Movie title:").grid(row=3, column=0, padx=5, pady=6, sticky="e")

        self.movie_title = tk.Entry(form, width=51); self.movie_title.grid(row=3, column=1, columnspan=3, padx=5, pady=6, sticky="w")

        tk.Label(form, text="Date:").grid(row=4, column=0, padx=5, pady=6, sticky="e")

        self.movie_date = tk.Entry(form, width=16); self.movie_date.grid(row=4, column=1, padx=5, pady=6, sticky="w")

        tk.Button(form, text="Calendar", command=lambda: self.open_calendar_picker(self.movie_date)).grid(row=4, column=2, padx=4, pady=6, sticky="w")

        tk.Label(form, text="Time:").grid(row=5, column=0, padx=5, pady=6, sticky="e")

        movie_time_row = tk.Frame(form); movie_time_row.grid(row=5, column=1, columnspan=2, padx=5, pady=6, sticky="w")

        self.movie_hour = tk.Spinbox(movie_time_row, from_=1, to=12, width=4, format="%.0f"); self.movie_hour.pack(side="left")

        tk.Label(movie_time_row, text=":").pack(side="left")

        self.movie_minute = tk.Spinbox(movie_time_row, from_=0, to=59, width=4, format="%02.0f"); self.movie_minute.pack(side="left")

        self.movie_ampm = ttk.Combobox(movie_time_row, state="readonly", width=5, values=["AM", "PM"]); self.movie_ampm.pack(side="left", padx=(6, 0)); self.movie_ampm.set("AM")

        tk.Button(movie_time_row, text="Clock", command=lambda: self.open_clock_picker(self.movie_hour, self.movie_minute, self.movie_ampm)).pack(side="left", padx=6)

        self.movie_hour.delete(0, tk.END); self.movie_hour.insert(0, "8"); self.movie_minute.delete(0, tk.END); self.movie_minute.insert(0, "0")

        tk.Label(form, text="Custom text:").grid(row=6, column=0, padx=5, pady=6, sticky="ne")

        self.movie_message = scrolledtext.ScrolledText(form, width=45, height=5); self.movie_message.grid(row=6, column=1, columnspan=3, padx=5, pady=6, sticky="w")

        tk.Button(form, text="Upload movie poster", command=self.choose_movie_image).grid(row=7, column=0, padx=5, pady=6)

        self.movie_image_label = tk.Label(form, text="No new poster selected (saved poster is kept)", anchor="w"); self.movie_image_label.grid(row=7, column=1, columnspan=3, padx=5, pady=6, sticky="w")

        tk.Button(f, text="Save movie announcement to Cloudflare R2", command=self.save_movie_announcement, width=38).pack(pady=16)



    def refresh_movie_ui(self):

        i = self.movie_server.current()

        if i < 0: return

        guild_id = self.guilds[i][0]; info = self.info.get(guild_id, {})

        self.movie_channel["values"] = [f"# {c['name']} ({c['id']})" for c in info.get("text_channels", [])]

        self.movie_roles.delete(0, tk.END)

        for role in info.get("roles", []): self.movie_roles.insert(tk.END, role["name"])

        config = self.settings.get("movie_announcements", {}).get(str(guild_id), {})

        for index, channel in enumerate(info.get("text_channels", [])):

            if channel["id"] == config.get("channel_id"): self.movie_channel.current(index); break

        for index, role in enumerate(info.get("roles", [])):

            if role["id"] in config.get("role_ids", []): self.movie_roles.selection_set(index)

        self.update_role_summary(self.movie_roles, self.movie_role_summary)

        self.movie_title.delete(0, tk.END); self.movie_title.insert(0, config.get("title", ""))

        self.movie_date.delete(0, tk.END); self.movie_date.insert(0, config.get("date", ""))

        hour12, minute, ampm = self.from_24h(config.get("time", "08:00")); self.movie_hour.delete(0, tk.END); self.movie_hour.insert(0, str(hour12)); self.movie_minute.delete(0, tk.END); self.movie_minute.insert(0, str(minute)); self.movie_ampm.set(ampm)

        self.movie_message.delete("1.0", tk.END); self.movie_message.insert("1.0", config.get("message", ""))



    def choose_movie_image(self):

        path = filedialog.askopenfilename(title="Choose movie poster", filetypes=[("Image files", "*.png *.jpg *.jpeg *.webp *.gif"), ("All files", "*.*")])

        if path and self.validate_image_upload(path):

            self.movie_image_path = path; self.movie_image_label.config(text=f"Selected: {path}")



    def save_movie_announcement(self):

        i, channel_index = self.movie_server.current(), self.movie_channel.current()

        if i < 0 or channel_index < 0: return messagebox.showwarning("Missing values", "Select a server and announcement channel.")

        try:

            scheduled = datetime.strptime(f"{self.movie_date.get().strip()} {self.movie_hour.get().strip()}:{self.movie_minute.get().strip()} {self.movie_ampm.get().strip()}", "%Y-%m-%d %I:%M %p")

        except ValueError:

            return messagebox.showwarning("Invalid schedule", "Use Calendar and Clock to choose a valid date and 12-hour AM/PM time.")

        guild_id = str(self.guilds[i][0]); info = self.info.get(self.guilds[i][0], {}); roles = info.get("roles", [])

        existing = self.settings.get("movie_announcements", {}).get(guild_id, {})

        config = {"id": existing.get("id") or uuid.uuid4().hex, "channel_id": info["text_channels"][channel_index]["id"], "role_ids": [roles[j]["id"] for j in self.movie_roles.curselection()], "title": self.movie_title.get().strip(), "date": scheduled.strftime("%Y-%m-%d"), "time": scheduled.strftime("%H:%M"), "message": self.movie_message.get("1.0", tk.END).strip(), "image_id": existing.get("image_id", ""), "enabled": True, "timezone": self.local_timezone}

        if self.movie_image_path:

            content_type, data = self.prepare_greeting_image(self.movie_image_path); config["image_id"] = f"{guild_id}_movie_{config['id']}"; self.store.save_notification_image(config["image_id"], content_type, data)

        self.settings.setdefault("movie_announcements", {})[guild_id] = config; self.persist_settings(); self.movie_image_path = None

        self.movie_image_label.config(text="Saved to Cloudflare R2")

        messagebox.showinfo("Saved", "Movie announcement saved. hosted worker synchronizes within 10 seconds.")

    def notifications_ui(self):

        f = self.notifications_tab; self.notif_image_path = None

        tk.Label(f, text="Role Notifications", font=("TkDefaultFont", 12, "bold")).pack(pady=(14, 4))

        tk.Label(f, text="Pick a channel, role(s), local time, and an optional image. Leave Custom message empty to send only the role mention; add text when you want a full announcement. GUI and hosted worker use the same Cloudflare R2 schedule, so each notification is sent exactly once.", wraplength=740, justify="center").pack(pady=(0, 10))



        top = tk.Frame(f); top.pack(fill="x", padx=20)

        tk.Label(top, text="Server:").grid(row=0, column=0, padx=5, pady=5, sticky="e")

        self.notif_server = ttk.Combobox(top, state="readonly", width=42); self.notif_server.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        self.notif_server.bind("<<ComboboxSelected>>", lambda event: self.refresh_notif_ui())



        body = tk.Frame(f); body.pack(fill="both", expand=True, padx=20, pady=(6, 0))

        left = tk.Frame(body); left.pack(side="left", fill="both", expand=True, padx=(0, 12))

        tk.Label(left, text="Scheduled notifications on this server:").pack(anchor="w")

        self.notif_list = tk.Listbox(left, height=8, exportselection=False); self.notif_list.pack(fill="both", expand=True, pady=4)

        self.notif_list.bind("<<ListboxSelect>>", lambda event: self.load_notif_entry_to_form())

        list_buttons = tk.Frame(left); list_buttons.pack(fill="x")

        tk.Button(list_buttons, text="New notification", command=self.clear_notif_form, width=18).grid(row=0, column=0, padx=4, pady=4)

        tk.Button(list_buttons, text="Delete selected", command=self.delete_notif_entry, width=18).grid(row=0, column=1, padx=4, pady=4)



        right = tk.Frame(body); right.pack(side="left", fill="both", expand=True)

        tk.Label(right, text="Channel:").grid(row=0, column=0, padx=5, pady=5, sticky="e")

        self.notif_channel = ttk.Combobox(right, state="readonly", width=32); self.notif_channel.grid(row=0, column=1, columnspan=3, padx=5, pady=5, sticky="w")

        tk.Label(right, text="Role(s):").grid(row=1, column=0, padx=5, pady=5, sticky="ne")

        self.notif_roles = tk.Listbox(right, selectmode="multiple", height=6, exportselection=False, width=32)

        self.notif_roles.grid(row=1, column=1, columnspan=3, padx=5, pady=5, sticky="w"); self.notif_roles.bind("<<ListboxSelect>>", lambda event: self.update_role_summary(self.notif_roles, self.notif_role_summary))

        self.notif_role_summary=tk.Label(right,text="Selected roles: none",fg=self.TEXT,bg="#080c16",relief="solid",bd=1,padx=10,pady=7,wraplength=330,justify="left"); self.notif_role_summary.grid(row=1,column=1,columnspan=3,padx=7,pady=(110,0),sticky="sw")

        tk.Label(right, text="Time:").grid(row=2, column=0, padx=5, pady=5, sticky="e")

        time_row = tk.Frame(right); time_row.grid(row=2, column=1, columnspan=3, padx=5, pady=5, sticky="w")

        self.notif_hour = tk.Spinbox(time_row, from_=1, to=12, width=4, format="%.0f"); self.notif_hour.pack(side="left")

        tk.Label(time_row, text=":").pack(side="left")

        self.notif_minute = tk.Spinbox(time_row, from_=0, to=59, width=4, format="%02.0f"); self.notif_minute.pack(side="left")

        self.notif_ampm = ttk.Combobox(time_row, state="readonly", width=5, values=["AM", "PM"]); self.notif_ampm.pack(side="left", padx=(6, 0)); self.notif_ampm.current(0); tk.Button(time_row, text="Clock", command=lambda: self.open_clock_picker(self.notif_hour, self.notif_minute, self.notif_ampm)).pack(side="left", padx=6)



        tk.Label(right, text="Repeat on:").grid(row=3, column=0, padx=5, pady=5, sticky="ne")

        days_frame = tk.Frame(right); days_frame.grid(row=3, column=1, columnspan=3, padx=5, pady=5, sticky="w")

        self.notif_day_vars = {code: tk.BooleanVar(value=False) for code in ("sat", "sun", "mon", "tue", "wed", "thu", "fri")}

        self.notif_day_checks = {}

        for i, (code, label) in enumerate((("sat", "Sat"), ("sun", "Sun"), ("mon", "Mon"), ("tue", "Tue"), ("wed", "Wed"), ("thu", "Thu"), ("fri", "Fri"))):

            cb = tk.Checkbutton(days_frame, text=label, variable=self.notif_day_vars[code], command=self.refresh_notif_schedule_state)

            cb.grid(row=0, column=i, padx=2); self.notif_day_checks[code] = cb

        self.notif_everyday_var = tk.BooleanVar(value=True)

        self.notif_everyday_check = tk.Checkbutton(days_frame, text="Everyday", variable=self.notif_everyday_var, command=self.refresh_notif_schedule_state)

        self.notif_everyday_check.grid(row=1, column=0, columnspan=7, sticky="w", pady=(4, 0))

        tk.Label(right, text="Or one-time date\n(YYYY-MM-DD, optional):", justify="right").grid(row=4, column=0, padx=5, pady=5, sticky="e")

        self.notif_date_entry = tk.Entry(right, width=16)

        self.notif_date_entry.grid(row=4, column=1, padx=5, pady=5, sticky="w")

        tk.Button(right, text="Calendar", command=lambda: self.open_calendar_picker(self.notif_date_entry, self.refresh_notif_schedule_state)).grid(row=4, column=2, padx=4, pady=5, sticky="w")

        self.notif_date_entry.bind("<KeyRelease>", lambda event: self.refresh_notif_schedule_state())

        tk.Label(right, text="A day, Everyday, and a specific date cannot be combined - pick one.", wraplength=380, justify="left", fg="#555").grid(row=5, column=1, columnspan=3, padx=5, sticky="w")


        tk.Label(right, text="Custom message (optional):").grid(row=6, column=0, padx=5, pady=5, sticky="e")

        self.notif_message = tk.Entry(right, width=38); self.notif_message.grid(row=6, column=1, columnspan=3, padx=5, pady=5, sticky="w")

        tk.Button(right, text="Upload image", command=self.choose_notif_image).grid(row=7, column=0, padx=5, pady=5, sticky="e")

        self.notif_image_label = tk.Label(right, text="No new image selected (saved image is kept)", anchor="w")

        self.notif_image_label.grid(row=7, column=1, columnspan=3, padx=5, pady=5, sticky="w")

        self.notif_preview = tk.Label(right, text="No image saved", anchor="w")

        self.notif_preview.grid(row=8, column=1, columnspan=3, padx=5, pady=(0, 6), sticky="w")

        self.notif_enabled = tk.BooleanVar(value=True)

        tk.Checkbutton(right, text="Enabled", variable=self.notif_enabled).grid(row=9, column=1, sticky="w", pady=5)

        tk.Button(right, text="Add / update this notification", command=self.add_or_update_notification, width=28).grid(row=10, column=1, columnspan=3, pady=(10, 4), sticky="w")

        for widget, value in ((self.notif_hour, 8), (self.notif_minute, 0)):

            widget.delete(0, tk.END); widget.insert(0, str(value))



        tk.Button(f, text="Save all role notifications to Cloudflare R2", command=self.save_role_notifications, width=38).pack(pady=14)

        self.refresh_notif_schedule_state()



    def refresh_notif_schedule_state(self):

        """Enforce mutual exclusivity: a specific date OR Everyday OR one/more

        weekdays - never a combination. Called after every checkbox click and

        every keystroke in the date box."""

        has_date = bool(self.notif_date_entry.get().strip())

        if has_date:

            for var in self.notif_day_vars.values(): var.set(False)

            self.notif_everyday_var.set(False)

            for cb in self.notif_day_checks.values(): cb.config(state="disabled")

            self.notif_everyday_check.config(state="disabled")

            return

        everyday = self.notif_everyday_var.get()

        any_day = any(var.get() for var in self.notif_day_vars.values())

        self.notif_date_entry.config(state="disabled" if (everyday or any_day) else "normal")

        if everyday:

            for var in self.notif_day_vars.values(): var.set(False)

            for cb in self.notif_day_checks.values(): cb.config(state="disabled")

            self.notif_everyday_check.config(state="normal")

        elif any_day:

            self.notif_everyday_var.set(False)

            self.notif_everyday_check.config(state="disabled")

            for cb in self.notif_day_checks.values(): cb.config(state="normal")

        else:

            for cb in self.notif_day_checks.values(): cb.config(state="normal")

            self.notif_everyday_check.config(state="normal")



    def current_notif_guild_id(self):

        i = self.notif_server.current()

        if i < 0: return None

        return self.guilds[i][0]



    def notif_summary(self, guild_id, entry):

        info = self.info.get(guild_id, {})

        channel_name = next((c["name"] for c in info.get("text_channels", []) if c["id"] == entry.get("channel_id")), entry.get("channel_id", "?"))

        role_names = [r["name"] for r in info.get("roles", []) if r["id"] in (entry.get("role_ids") or [])]

        state = "" if entry.get("enabled", True) else " (disabled)"

        entry_date = entry.get("date", "")

        days = entry.get("days") or (["everyday"] if not entry_date else [])

        if entry_date: schedule = entry_date

        elif "everyday" in days: schedule = "Everyday"

        else: schedule = "/".join(day.title() for day in days) or "Everyday"

        return f"#{channel_name} - {', '.join(role_names) or '?'} - {schedule} - {entry.get('time_12h', entry.get('time', '?'))}{state}"



    def refresh_notif_ui(self):

        guild_id = self.current_notif_guild_id()

        if guild_id is None: return

        info = self.info.get(guild_id, {})

        self.notif_channel["values"] = [f"# {c['name']} ({c['id']})" for c in info.get("text_channels", [])]

        self.notif_roles.delete(0, tk.END)

        for role in info.get("roles", []):

            self.notif_roles.insert(tk.END, role["name"])

        self.notif_entries.setdefault(str(guild_id), self.settings.get("role_notifications", {}).get(str(guild_id), []))

        self.update_role_summary(self.notif_roles, self.notif_role_summary)

        self.clear_notif_form()

        self.refresh_notif_list()



    def refresh_notif_list(self):

        guild_id = self.current_notif_guild_id()

        self.notif_list.delete(0, tk.END)

        if guild_id is None: return

        for entry in self.notif_entries.get(str(guild_id), []):

            self.notif_list.insert(tk.END, self.notif_summary(guild_id, entry))



    def clear_notif_form(self):

        self.notif_edit_index = None

        self.notif_channel.set(""); self.notif_roles.selection_clear(0, tk.END); self.update_role_summary(self.notif_roles, self.notif_role_summary)

        self.notif_message.delete(0, tk.END)

        self.notif_hour.delete(0, tk.END); self.notif_hour.insert(0, "8")

        self.notif_minute.delete(0, tk.END); self.notif_minute.insert(0, "00")

        self.notif_ampm.current(0); self.notif_enabled.set(True)

        for var in self.notif_day_vars.values(): var.set(False)

        self.notif_date_entry.config(state="normal"); self.notif_date_entry.delete(0, tk.END)

        self.notif_everyday_var.set(True)

        self.refresh_notif_schedule_state()

        self.notif_image_path = None

        self.notif_image_label.config(text="No new image selected (saved image is kept)")

        self.show_notif_preview()



    def load_notif_entry_to_form(self):

        guild_id = self.current_notif_guild_id()

        selection = self.notif_list.curselection()

        if guild_id is None or not selection: return

        index = selection[0]; entry = self.notif_entries.get(str(guild_id), [])[index]

        self.notif_edit_index = index

        info = self.info.get(guild_id, {})

        channels = info.get("text_channels", [])

        for i, channel in enumerate(channels):

            if channel["id"] == entry.get("channel_id"): self.notif_channel.current(i); break

        self.notif_roles.selection_clear(0, tk.END)

        roles = info.get("roles", [])

        for i, role in enumerate(roles):

            if role["id"] in (entry.get("role_ids") or []): self.notif_roles.selection_set(i)

        self.update_role_summary(self.notif_roles, self.notif_role_summary)

        hour12, minute, ampm = self.from_24h(entry.get("time", "08:00"))

        self.notif_hour.delete(0, tk.END); self.notif_hour.insert(0, str(hour12))

        self.notif_minute.delete(0, tk.END); self.notif_minute.insert(0, f"{minute:02d}")

        self.notif_ampm.set(ampm)

        entry_date = entry.get("date", "") or ""

        days = entry.get("days") or (["everyday"] if not entry_date else [])

        self.notif_date_entry.config(state="normal"); self.notif_date_entry.delete(0, tk.END)

        if entry_date: self.notif_date_entry.insert(0, entry_date)

        for code, var in self.notif_day_vars.items(): var.set(code in days)

        self.notif_everyday_var.set("everyday" in days)

        self.refresh_notif_schedule_state()

        self.notif_message.delete(0, tk.END); self.notif_message.insert(0, entry.get("message", ""))

        self.notif_enabled.set(bool(entry.get("enabled", True)))

        self.notif_image_path = None

        self.notif_image_label.config(text="No new image selected (saved image is kept)")

        self.load_notif_preview(entry.get("image_id", ""))



    def show_notif_preview(self, data=None, text=None):

        if not data:

            self.notif_preview.config(image="", text=text or "No image saved")

            self.notif_preview_image = None

            return

        try:

            image = Image.open(io.BytesIO(data)); image.thumbnail((260, 130)); photo = ImageTk.PhotoImage(image.copy())

            self.notif_preview.config(image=photo, text=""); self.notif_preview_image = photo

        except Exception as exc:

            self.notif_preview.config(image="", text=f"Could not preview image: {exc}"); self.notif_preview_image = None



    def load_notif_preview(self, image_id):

        if not image_id: return self.show_notif_preview()

        try:

            image = self.store.get_notification_image(image_id)

            if not image: return self.show_notif_preview(text="Saved image is no longer available")

            self.show_notif_preview(image[1])

        except Exception as exc:

            self.show_notif_preview(text=f"Could not load saved image: {exc}")



    def choose_notif_image(self):

        path = filedialog.askopenfilename(title="Choose notification image", filetypes=[("Image files", "*.png *.jpg *.jpeg *.webp *.gif"), ("All files", "*.*")])

        if not path or not self.validate_image_upload(path): return

        try:

            with open(path, "rb") as image_file: self.show_notif_preview(image_file.read())

            self.notif_image_path = path; self.notif_image_label.config(text=f"Selected: {path}")

        except Exception as exc: messagebox.showerror("Image error", f"Could not open this image: {exc}")



    @staticmethod

    def to_24h(hour12, minute, ampm):

        hour12 = hour12 % 12

        hour24 = hour12 + (12 if ampm == "PM" else 0)

        return f"{hour24:02d}:{minute:02d}"



    @staticmethod

    def from_24h(time_str):

        hour24, minute = (int(part) for part in time_str.split(":"))

        ampm = "PM" if hour24 >= 12 else "AM"

        hour12 = hour24 % 12 or 12

        return hour12, minute, ampm



    def add_or_update_notification(self):

        guild_id = self.current_notif_guild_id()

        channel_index = self.notif_channel.current()

        role_indices = self.notif_roles.curselection()

        if guild_id is None or channel_index < 0 or not role_indices:

            return messagebox.showwarning("Missing values", "Select a server, a channel, and at least one role.")

        try:

            hour12, minute = int(self.notif_hour.get()), int(self.notif_minute.get())

            if not (1 <= hour12 <= 12 and 0 <= minute <= 59): raise ValueError

        except ValueError:

            return messagebox.showwarning("Invalid time", "Hour must be 1-12 and minute 0-59.")

        ampm = self.notif_ampm.get() or "AM"

        entry_date = self.notif_date_entry.get().strip()

        selected_days = [code for code, var in self.notif_day_vars.items() if var.get()]

        if entry_date:

            try:

                datetime.strptime(entry_date, "%Y-%m-%d")

            except ValueError:

                return messagebox.showwarning("Invalid date", "Date must be in YYYY-MM-DD format, e.g. 2026-09-20.")

            days_value = []

        elif self.notif_everyday_var.get():

            days_value, entry_date = ["everyday"], ""

        elif selected_days:

            days_value, entry_date = selected_days, ""

        else:

            return messagebox.showwarning("Missing schedule", "Pick Everyday, at least one day, or a specific date.")

        info = self.info.get(guild_id, {})

        channels, roles = info.get("text_channels", []), info.get("roles", [])

        entries = self.notif_entries.setdefault(str(guild_id), [])

        existing = entries[self.notif_edit_index] if self.notif_edit_index is not None and 0 <= self.notif_edit_index < len(entries) else {}

        entry_id = existing.get("id") or uuid.uuid4().hex

        image_id = existing.get("image_id", "")

        if self.notif_image_path:

            try:

                content_type, data = self.prepare_greeting_image(self.notif_image_path)

                image_id = f"{guild_id}_notif_{entry_id}"

                self.store.save_notification_image(image_id, content_type, data)

            except Exception as exc:

                return messagebox.showerror("Image error", f"Could not save image: {exc}")

        entry = {

            "id": entry_id, "channel_id": channels[channel_index]["id"],

            "role_ids": [roles[i]["id"] for i in role_indices],

            "time": self.to_24h(hour12, minute, ampm),

            "time_12h": f"{hour12}:{minute:02d} {ampm}",

            "days": days_value,

            "date": entry_date,

            "message": self.notif_message.get().strip(),

            "enabled": self.notif_enabled.get(),

            "image_id": image_id, "timezone": self.local_timezone,

        }

        if self.notif_edit_index is not None and 0 <= self.notif_edit_index < len(entries):

            entries[self.notif_edit_index] = entry

        else:

            entries.append(entry)

        self.notif_image_path = None

        self.refresh_notif_list(); self.clear_notif_form()



    def delete_notif_entry(self):

        guild_id = self.current_notif_guild_id()

        selection = self.notif_list.curselection()

        if guild_id is None or not selection:

            return messagebox.showwarning("Nothing selected", "Select a notification in the list first.")

        del self.notif_entries[str(guild_id)][selection[0]]

        self.refresh_notif_list(); self.clear_notif_form()



    def save_role_notifications(self):

        merged = dict(self.settings.get("role_notifications", {}))

        for guild_id, entries in self.notif_entries.items():

            if entries: merged[guild_id] = entries

            else: merged.pop(guild_id, None)

        self.settings["role_notifications"] = merged

        self.persist_settings()

        messagebox.showinfo("Saved", "Role notification schedule saved using your local time zone. hosted worker synchronizes within 10 seconds.")



    def default_roles_ui(self):

        f = self.default_roles_tab

        tk.Label(f, text="Default Role (Auto-Role on Join)", font=("TkDefaultFont", 12, "bold")).pack(pady=(18, 5))

        tk.Label(f, text="New members automatically get the role(s) you select below the moment they join. Only roles below the bot's highest role are shown. Use \"Apply to all existing members\" to give the same role(s) to everyone already in the server, and remove previously managed roles you unselect.", wraplength=720, justify="center").pack(pady=(0, 12))

        top = tk.Frame(f); top.pack(pady=(0, 6))

        tk.Label(top, text="Server:").grid(row=0, column=0, padx=5, pady=5, sticky="e")

        self.default_role_server = ttk.Combobox(top, state="readonly", width=42); self.default_role_server.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        self.default_role_server.bind("<<ComboboxSelected>>", lambda event: self.refresh_default_role_ui())

        tk.Label(f, text="Role(s) to auto-assign - select one or more:").pack(pady=(6, 3))

        self.default_role_options = []

        self.default_role_list = tk.Listbox(f, selectmode="multiple", width=50, height=12, exportselection=False)

        self.default_role_list.pack(pady=(0, 6)); self.default_role_list.bind("<<ListboxSelect>>", lambda event: self.update_role_summary(self.default_role_list, self.default_role_summary))

        self.default_role_summary=tk.Label(f,text="Selected roles: none",fg=self.TEXT,bg="#080c16",relief="solid",bd=1,padx=10,pady=7,wraplength=700,justify="center"); self.default_role_summary.pack(pady=(0,10))

        tk.Button(f, text="Save default role(s) to Cloudflare R2", command=self.save_default_roles, width=32).pack(pady=(0, 10))

        one_member = tk.Frame(f); one_member.pack(pady=(0, 10))

        tk.Label(one_member, text="Apply saved Auto Role to member ID:").grid(row=0, column=0, padx=5, pady=5)

        self.default_role_member_id = tk.Entry(one_member, width=24); self.default_role_member_id.grid(row=0, column=1, padx=5, pady=5)

        tk.Button(one_member, text="Verify ID + apply", command=self.apply_default_roles_to_id).grid(row=0, column=2, padx=5, pady=5)

        ttk.Separator(f, orient="horizontal").pack(fill="x", padx=28, pady=6)

        tk.Label(f, text="Backfill: apply the saved default role(s) to everyone already in the server (one-time).", wraplength=700, justify="center").pack(pady=(12, 6))

        tk.Button(f, text="Apply to all existing members", command=self.apply_default_roles_all, width=32).pack(pady=6)

        self.default_role_progress = ttk.Progressbar(f, length=420, mode="determinate", maximum=100)

        self.default_role_progress.pack(pady=(8, 2))

        self.default_role_progress_label = tk.Label(f, text="")

        self.default_role_progress_label.pack()



    def current_default_role_guild_id(self):

        i = self.default_role_server.current()

        if i < 0: return None

        return self.guilds[i][0]



    def refresh_default_role_ui(self):

        guild_id = self.current_default_role_guild_id()

        if guild_id is None: return

        info = self.info.get(guild_id, {})

        self.default_role_list.delete(0, tk.END)

        saved = self.settings.get("default_roles", {}).get(str(guild_id), [])

        self.default_role_options = [role for role in info.get("roles", []) if role.get("manageable", False)]

        for i, role in enumerate(self.default_role_options):

            self.default_role_list.insert(tk.END, role["name"])

            if role["id"] in saved: self.default_role_list.selection_set(i)

        self.update_role_summary(self.default_role_list, self.default_role_summary)

        self.default_role_progress["value"] = 0

        self.default_role_progress_label.config(text="")



    def save_default_roles(self):

        guild_id = self.current_default_role_guild_id()

        if guild_id is None: return messagebox.showwarning("Missing server", "Select a server first.")

        info = self.info.get(guild_id)

        if not info:

            return messagebox.showwarning("Roles still loading", "Server roles have not arrived yet. Wait a moment, then try again.")

        selected = [self.default_role_options[i]["id"] for i in self.default_role_list.curselection() if i < len(self.default_role_options)]

        self.adopt_settings(self.store.save_default_roles(guild_id, selected))

        if self.runtime: self.runtime.update_settings(self.settings)

        self.update_role_summary(self.default_role_list, self.default_role_summary)

        messagebox.showinfo("Saved", "Auto Roles saved. Apply to all also removes unselected Auto Roles. hosted worker synchronizes within 10 seconds.")



    def apply_default_roles_to_id(self):

        guild_id = self.current_default_role_guild_id(); user_id = self.default_role_member_id.get().strip()

        if guild_id is None or not user_id.isdigit():

            return messagebox.showwarning("Member ID missing", "Select a server and enter a numeric Discord member ID.")

        if not self.runtime or not self.runtime.loop:

            return messagebox.showwarning("GUI bot offline", "Connect the GUI bot first so the member ID can be verified.")

        future = asyncio.run_coroutine_threadsafe(self.runtime.apply_default_roles_to_member_id(guild_id, user_id), self.runtime.loop)

        def done(task):

            try: self.q.put(("auto_role_member_applied", task.result()))

            except Exception as exc: self.q.put(("log", f"Auto Role verification/apply failed: {exc}"))

        future.add_done_callback(done)

    def apply_default_roles_all(self):

        guild_id = self.current_default_role_guild_id()

        if guild_id is None: return messagebox.showwarning("Missing server", "Select a server first.")

        if not self.settings.get("default_role_managed_ids", {}).get(str(guild_id), self.settings.get("default_roles", {}).get(str(guild_id), [])):

            return messagebox.showwarning("No Auto Role saved", "Save an Auto Role selection first.")

        if not self.runtime or not self.runtime.loop:

            return messagebox.showwarning("GUI bot offline", "Connect the GUI bot first (Connection tab) so Discord can update existing members.")

        self.default_role_progress["value"] = 0

        self.default_role_progress_label.config(text="Starting...")

        def progress(done, total):

            percent = int(done * 100 / total) if total else 100

            self.q.put(("default_role_progress", (percent, done, total)))

        future = asyncio.run_coroutine_threadsafe(self.runtime.apply_default_roles_bulk(guild_id, progress), self.runtime.loop)

        def done(task):

            try:

                updated, total, added, removed, skipped = task.result()

                suffix = f", {skipped} skipped" if skipped else ""

                self.q.put(("log", f"Auto Roles reconciled for {updated}/{total} members: {added} added, {removed} removed{suffix}."))

                self.q.put(("default_role_progress", (100, total, total)))

            except Exception as exc:

                self.q.put(("log", f"Apply default role failed: {exc}"))

                self.q.put(("default_role_progress", (0, 0, 0)))

        future.add_done_callback(done)



    def log(self, message):
        self.shared_logger(message)



    def open_invite(self):

        if not self.invite_url: return messagebox.showwarning("Invite unavailable","Connect the GUI bot first.")

        webbrowser.open(self.invite_url)



    def load_logs(self):

        try:

            records=self.store.recent_logs()

            self.logs.config(state="normal"); self.logs.delete("1.0",tk.END)

            for record in records:self.logs.insert(tk.END,f"[{record['at']}] {record['source']}: {record['message']}\n")

            self.logs.config(state="disabled")

        except Exception as exc: messagebox.showerror("Log error",str(exc))



    def clear_logs_older_than_30_days(self):

        """Apply the shared 30-day log retention rule without freezing the GUI."""

        confirmed = messagebox.askyesno(

            "Clear old Cloudflare R2 logs",

            "Permanently delete all shared Cloudflare R2 logs older than 30 days?\n\nThis cannot be undone.",

        )

        if not confirmed:

            return

        def clear():

            try:

                self.q.put(("logs_cleared", self.store.delete_logs_older_than(30)))

            except Exception as exc:

                self.q.put(("logs_clear_error", str(exc)))

        threading.Thread(target=clear, daemon=True).start()



    @staticmethod

    def format_bytes(size):

        value=float(size)

        for unit in ("B","KB","MB","GB","TB"):

            if value<1024 or unit=="TB": return f"{value:.1f} {unit}" if unit!="B" else f"{int(value)} B"

            value/=1024



    def database_usage(self):

        self.database_status.config(text="Checking Cloudflare storage usage…")

        self.database_progress["value"]=0

        def work():

            try:

                d1_size,approximate=self.store.get_database_usage()

                r2_size=self.store.get_r2_usage()

                self.q.put(("db_usage",(d1_size,r2_size,approximate)))

            except Exception as exc: self.q.put(("db_error",str(exc)))

        threading.Thread(target=work,daemon=True).start()



    def export_database(self):

        path=filedialog.asksaveasfilename(title="Export complete bot backup",defaultextension=".json",filetypes=[("JSON backup","*.json")])

        if not path:return

        self.database_status.config(text="Exporting D1 records and R2 files…")

        self.database_progress["value"]=0

        def work():

            try:

                def progress(done,total,label): self.q.put(("db_progress",(int(done*90/total),f"{label}: {done}/{total}")))

                backup=self.store.export_snapshot(progress=progress)

                encoder=json.JSONEncoder(separators=(",",":"))

                total_bytes=sum(len(chunk.encode("utf-8")) for chunk in encoder.iterencode(backup))

                written=0; last_percent=90

                with open(path,"w",encoding="utf-8") as handle:

                    for chunk in encoder.iterencode(backup):

                        handle.write(chunk); written+=len(chunk.encode("utf-8"))

                        percent=90+int(written*9/max(1,total_bytes))

                        if percent!=last_percent:

                            self.q.put(("db_progress",(percent,"Writing JSON backup")))

                            last_percent=percent

                self.q.put(("db_export_done",(path,len(backup["d1_records"]),len(backup["r2_objects"]))))

            except Exception as exc:self.q.put(("db_error",str(exc)))

        threading.Thread(target=work,daemon=True).start()



    def import_database(self):

        path=filedialog.askopenfilename(title=f"Choose a {APP_CONFIG.bot_app_name} JSON backup",filetypes=[("JSON backup","*.json")])

        if not path:return

        if not messagebox.askyesno("Import backup","Restore all records and files from this backup? Matching records/files in this database will be replaced by the backup. Records/files not included in the backup will remain. If interrupted, select this same file again to continue from the saved progress."):return

        self.database_status.config(text="Importing backup…")

        self.database_progress["value"]=0

        def work():

            try:

                with open(path,"r",encoding="utf-8") as handle: backup=json.load(handle)

                if backup.get("format")!="meta-bot-backup" or backup.get("version")!=1: raise ValueError(f"This JSON is not a supported {APP_CONFIG.bot_app_name} backup.")

                if str(backup.get("control_id",""))!=str(self.store.control_id): raise ValueError("Backup BOT_CONTROL_ID does not match this app. Set the same BOT_CONTROL_ID as the source backup, then retry.")

                items=[("d1",item) for item in backup.get("d1_records",[])]+[("r2",item) for item in backup.get("r2_objects",[])]

                checkpoint=path+".import-progress"

                start=0

                if os.path.isfile(checkpoint):

                    with open(checkpoint,"r",encoding="utf-8") as handle:start=int(json.load(handle).get("next",0))

                if not 0<=start<=len(items): raise ValueError("Import progress file is invalid; remove the .import-progress file to restart.")

                for index in range(start,len(items)):

                    kind,item=items[index]; self.store.import_snapshot_item(kind,item)

                    with open(checkpoint,"w",encoding="utf-8") as handle:json.dump({"next":index+1,"total":len(items)},handle)

                    percent=int((index+1)*100/max(1,len(items)))

                    self.q.put(("db_progress",(percent,f"Importing {index+1}/{len(items)}")))

                if os.path.exists(checkpoint):os.remove(checkpoint)

                self.q.put(("db_import_done",(len(items),start)))

            except Exception as exc:self.q.put(("db_error",str(exc)))

        threading.Thread(target=work,daemon=True).start()



    @staticmethod
    def _merge_settings(base, local, remote):
        if local == base:
            return copy.deepcopy(remote)
        if isinstance(base, dict) and isinstance(local, dict) and isinstance(remote, dict):
            merged = copy.deepcopy(remote)
            for key in set(base) | set(local):
                old = base.get(key, object())
                new = local.get(key, object())
                if new == old:
                    continue
                if key not in local:
                    merged.pop(key, None)
                elif key in base and key in remote:
                    merged[key] = App._merge_settings(base[key], local[key], remote[key])
                else:
                    merged[key] = copy.deepcopy(new)
            return merged
        return copy.deepcopy(local)

    def persist_settings(self):
        latest = self.store.get_settings()
        merged = self._merge_settings(self.settings_baseline, self.settings, latest)
        merged["settings_revision"] = latest.get("settings_revision", 0)
        self.adopt_settings(self.store.save_settings(merged, baseline=self.settings_baseline))

        return self.settings



    def adopt_settings(self, settings):

        self.settings = settings

        self.settings_baseline = copy.deepcopy(settings)

        if self.runtime:
            self.runtime.update_settings(self.settings)
    def save_connection(self):

        if not self.bot_token: messagebox.showwarning("Token missing","Set DISCORD_BOT_TOKEN in your local .env file, then restart the GUI."); return False

        self.settings["auto_connect"]=self.auto.get()

        self.persist_settings()

        messagebox.showinfo("Saved","Encrypted settings saved. hosted worker synchronizes within 10 seconds."); return True



    def connect(self):

        if self.thread or not self.save_connection(): return

        def run():

            try:

                self.runtime=ModeratorBot(self.store,"gui",self.log,lambda kind,data:self.q.put((kind,data)))

                asyncio.run(self.runtime.run(self.bot_token))

            except Exception as exc: self.q.put(("log",f"Connection error: {exc}"))

            finally: self.thread=None; self.runtime=None; self.q.put(("status","Disconnected"))

        self.thread=threading.Thread(target=run,daemon=True); self.thread.start(); self.set_status("Connecting")



    def show_permission_roles(self, server, role_list, setting_name):

        i=server.current()

        if i<0:return

        gid=self.guilds[i][0]; data=self.info.get(gid,{}) ; allowed=set(self.settings.get(setting_name,{}).get(str(gid),[])); role_list.delete(0,tk.END)

        for n,role in enumerate(data.get("roles",[])):

            role_list.insert(tk.END,role["name"])

            if role["id"] in allowed: role_list.selection_set(n)

        summaries={self.chat_roles:self.chat_role_summary,self.link_roles:self.link_role_summary,self.drag_roles:self.drag_role_summary}

        self.update_role_summary(role_list, summaries[role_list])



    def update_role_summary(self, role_list, label):

        names=[role_list.get(index) for index in role_list.curselection()]

        # Every role-based feature uses this status box. A numbered green list

        # makes already selected roles easy to confirm at a glance.

        summary = "Selected roles: none" if not names else "Selected roles:\n" + "\n".join(

            f"{number}. {name}" for number, name in enumerate(names, start=1)

        )

        label.config(text=summary, bg="#080c16", fg=self.SUCCESS, relief="solid", bd=1, padx=10, pady=7)



    def schedule_settings_sync(self):

        if not self.settings_sync_inflight:

            self.settings_sync_inflight=True

            def load():

                try: self.q.put(("settings_sync", self.store.get_settings()))

                except Exception as exc: self.q.put(("settings_sync_error", str(exc)))

            threading.Thread(target=load,daemon=True).start()

        self.root.after(10000,self.schedule_settings_sync)



    def show_chat_roles(self):

        self.show_permission_roles(self.chat_server, self.chat_roles, "chat_allowed_roles")



    def show_drag_roles(self):

        self.show_permission_roles(self.drag_server, self.drag_roles, "drag_allowed_roles")

    def show_link_roles(self):

        self.show_permission_roles(self.link_server, self.link_roles, "link_allowed_roles")



    def save_permission_roles(self, server, role_list, setting_name, feature_name):

        index = server.current()

        if index < 0 or index >= len(self.guilds):

            return messagebox.showwarning("Server missing", "Connect the GUI bot and select a server first.")

        guild_id = self.guilds[index][0]

        guild_data = self.info.get(guild_id)

        if not guild_data:

            return messagebox.showwarning("Roles still loading", "Server roles have not arrived yet. Wait a moment, then try again.")

        roles = guild_data.get("roles", [])

        selected_role_ids = [roles[row]["id"] for row in role_list.curselection() if row < len(roles)]

        self.adopt_settings(self.store.save_permission_roles(setting_name, guild_id, selected_role_ids))

        if self.runtime: self.runtime.update_settings(self.settings)

        messagebox.showinfo("Saved", f"{feature_name} permissions encrypted and saved. hosted worker synchronizes within 10 seconds.")



    def save_chat_roles(self):

        self.save_permission_roles(self.chat_server, self.chat_roles, "chat_allowed_roles", "Chat")



    def save_link_roles(self):

        self.save_permission_roles(self.link_server, self.link_roles, "link_allowed_roles", "Link")



    def save_drag_roles(self):

        self.save_permission_roles(self.drag_server, self.drag_roles, "drag_allowed_roles", "Voice Drag")

    def refresh_ticket_channels(self):

        i=self.ticket_server.current()

        if i<0:return

        guild_id=self.guilds[i][0]; channels=self.info.get(guild_id,{}).get("text_channels",[])

        values=[f"# {channel['name']} ({channel['id']})" for channel in channels]

        self.ticket_channel["values"]=values

        saved=str(self.settings.get("ticket_channels",{}).get(str(guild_id),""))

        for index,channel in enumerate(channels):

            if channel["id"]==saved: self.ticket_channel.current(index); break

        self.refresh_ticket_moderators()



    def render_ticket_moderators(self):

        if not hasattr(self, "ticket_moderator_selected"): return

        cached = {x["id"]: x for x in self.info.get(self.guilds[self.ticket_server.current()][0], {}).get("members", [])} if self.ticket_server.current() >= 0 else {}

        details = {**cached, **self.ticket_moderator_details}

        self.ticket_moderator_selected.delete(0, tk.END)

        for number, user_id in enumerate(self.ticket_moderator_ids, 1):

            member = details.get(user_id, {"display_name": "Verified user", "name": user_id})

            self.ticket_moderator_selected.insert(tk.END, f"{number}. {member['display_name']} (@{member['name']}) - {user_id}")



    def refresh_ticket_moderators(self):

        index = self.ticket_server.current()

        if index < 0: return

        self.ticket_moderator_ids = list(self.settings.get("ticket_moderators", {}).get(str(self.guilds[index][0]), []))

        if hasattr(self, "ticket_moderator_id"): self.ticket_moderator_id.delete(0, tk.END)

        self.render_ticket_moderators()



    def add_ticket_moderator_by_id(self):

        index, user_id = self.ticket_server.current(), self.ticket_moderator_id.get().strip()

        if index < 0 or not user_id.isdigit():

            return messagebox.showwarning("Member ID missing", "Select a server and enter a numeric Discord member ID.")

        if not self.runtime or not self.runtime.loop:

            return messagebox.showwarning("GUI bot offline", "Connect the GUI bot first so the member ID can be verified.")

        future = asyncio.run_coroutine_threadsafe(self.runtime.verify_guild_member(self.guilds[index][0], user_id), self.runtime.loop)

        def done(task):

            try:

                member = task.result()

                self.q.put(("ticket_moderator_verified", {"id": str(member.id), "name": str(member), "display_name": member.display_name}))

            except Exception as exc:

                self.q.put(("log", f"Staff verification failed: {exc}"))

        future.add_done_callback(done)

    def remove_ticket_moderator(self):

        selected = self.ticket_moderator_selected.curselection()

        if not selected: return messagebox.showwarning("Moderator missing", "Select a moderator to remove.")

        self.ticket_moderator_ids.pop(selected[0]); self.render_ticket_moderators()



    def save_ticket_moderators(self):

        index = self.ticket_server.current()

        if index < 0: return messagebox.showwarning("Server missing", "Connect the GUI bot and select a server first.")

        self.adopt_settings(self.store.save_ticket_moderators(self.guilds[index][0], self.ticket_moderator_ids))

        summary = "Ticket moderators saved." if self.ticket_moderator_ids else "Ticket staff list cleared. New tickets are disabled until a moderator is added."

        messagebox.showinfo("Saved", f"{summary} hosted worker synchronizes within 10 seconds.")

    def save_ticket_channel(self):

        server_index,channel_index=self.ticket_server.current(),self.ticket_channel.current()

        if server_index<0 or channel_index<0: return messagebox.showwarning("Ticket channel missing","Connect the GUI bot, then select a server and one text channel.")

        guild_id=self.guilds[server_index][0]; channels=self.info.get(guild_id,{}).get("text_channels",[])

        self.settings.setdefault("ticket_channels",{})[str(guild_id)]=channels[channel_index]["id"]

        self.persist_settings()

        messagebox.showinfo("Saved","The bot will post a Create Ticket button in that channel. hosted worker synchronizes within 10 seconds.")



    def refresh_voice_channels(self):

        i=self.voice_server.current()

        if i<0:return

        guild_id=self.guilds[i][0]; channels=self.info.get(guild_id,{}).get("voice_channels",[])

        self.voice_channel["values"]=[f"[Voice] {channel['name']} ({channel['id']})" for channel in channels]
        saved=str(self.settings.get("temp_voice_lobbies",{}).get(str(guild_id),{}).get("channel_id",""))

        for index,channel in enumerate(channels):

            if channel["id"]==saved: self.voice_channel.current(index); break



    def save_voice_lobby(self):

        server_index,channel_index=self.voice_server.current(),self.voice_channel.current()

        if server_index<0 or channel_index<0: return messagebox.showwarning("Voice channel missing","Connect the GUI bot, then select a server and a voice channel.")

        guild_id=self.guilds[server_index][0]; channels=self.info.get(guild_id,{}).get("voice_channels",[])

        self.settings.setdefault("temp_voice_lobbies",{})[str(guild_id)]={"channel_id":channels[channel_index]["id"]}

        self.persist_settings()

        messagebox.showinfo("Saved","Temporary voice lobby saved. hosted worker synchronizes within 10 seconds.")



    def refresh_level_channels(self):

        i=self.level_server.current()

        if i<0:return

        guild_id=self.guilds[i][0]; channels=self.info.get(guild_id,{}).get("text_channels",[])

        self.level_channel["values"]=[f"# {channel['name']} ({channel['id']})" for channel in channels]

        config=self.settings.get("leveling",{}).get(str(guild_id),{})

        self.level_enabled.set(bool(config.get("enabled",False)))

        for widget,key,default in ((self.level_xp,"xp_per_message",15),(self.level_cooldown,"cooldown_seconds",60),(self.level_base,"level_base",100),(self.level_multiplier,"level_multiplier",1.5),(self.voice_level_xp,"voice_xp_amount",10),(self.voice_level_interval,"voice_xp_interval_seconds",60)):

            widget.delete(0,tk.END); widget.insert(0,str(config.get(key,default)))

        self.voice_level_require_others.set(bool(config.get("voice_xp_require_others",True)))

        self.voice_level_ignore_deafened.set(bool(config.get("voice_xp_ignore_deafened",True)))

        for index,channel in enumerate(channels):

            if channel["id"]==str(config.get("channel_id","")):

                self.level_channel.current(index); break



    def save_leveling(self):

        server_index,channel_index=self.level_server.current(),self.level_channel.current()

        if server_index<0 or channel_index<0:

            return messagebox.showwarning("Level channel missing","Connect the GUI bot, then select a server and level-up text channel.")

        try:

            xp,cooldown,base=int(self.level_xp.get()),int(self.level_cooldown.get()),int(self.level_base.get())

            multiplier=float(self.level_multiplier.get())

            voice_xp,voice_interval=int(self.voice_level_xp.get()),int(self.voice_level_interval.get())

            if not (1<=xp<=100 and 5<=cooldown<=3600 and 10<=base<=10000 and 1.0<=multiplier<=3.0): raise ValueError

            if not (1<=voice_xp<=100 and 30<=voice_interval<=1800): raise ValueError

        except ValueError:

            return messagebox.showwarning("Invalid leveling values","XP must be 1-100, cooldown 5-3600, base 10-10000, multiplier 1.0-3.0, voice XP 1-100, and voice interval 30-1800.")

        guild_id=str(self.guilds[server_index][0]); channels=self.info.get(self.guilds[server_index][0],{}).get("text_channels",[])

        self.settings.setdefault("leveling",{})[guild_id]={

            "enabled":self.level_enabled.get(),"channel_id":channels[channel_index]["id"],

            "xp_per_message":xp,"cooldown_seconds":cooldown,"level_base":base,"level_multiplier":multiplier,

            "voice_xp_amount":voice_xp,"voice_xp_interval_seconds":voice_interval,

            "voice_xp_require_others":self.voice_level_require_others.get(),

            "voice_xp_ignore_deafened":self.voice_level_ignore_deafened.get(),

        }

        self.adopt_settings(self.store.save_leveling_config(guild_id,self.settings["leveling"][guild_id]))

        if self.runtime: self.runtime.update_settings(self.settings)

        messagebox.showinfo("Saved","Leveling settings (text + voice XP) are encrypted in the shared Cloudflare database. hosted worker synchronizes within 10 seconds.")



    def refresh_greeting_channels(self):

        i=self.greeting_server.current()

        if i<0:return

        guild_id=self.guilds[i][0]; channels=self.info.get(guild_id,{}).get("text_channels",[])

        values=[f"# {channel['name']} ({channel['id']})" for channel in channels]

        self.welcome_channel["values"],self.goodbye_channel["values"]=values,values

        config=self.settings.get("welcome_goodbye",{}).get(str(guild_id),{})

        for index,channel in enumerate(channels):

            if channel["id"]==str(config.get("welcome_channel_id","")): self.welcome_channel.current(index)

            if channel["id"]==str(config.get("goodbye_channel_id","")): self.goodbye_channel.current(index)

        self.welcome_message.delete(0,tk.END); self.welcome_message.insert(0,config.get("welcome_message",""))

        self.goodbye_message.delete(0,tk.END); self.goodbye_message.insert(0,config.get("goodbye_message",""))

        self.load_greeting_preview("welcome",config.get("welcome_image_id", ""))

        self.load_greeting_preview("goodbye",config.get("goodbye_image_id", ""))



    def show_greeting_preview(self, kind, data=None, text=None):

        label=getattr(self,f"{kind}_preview")

        if not data:

            label.config(image="",text=text or f"No {kind} image saved")

            setattr(self,f"{kind}_preview_image",None)

            return

        try:

            image=Image.open(io.BytesIO(data)); image.thumbnail((260,130)); photo=ImageTk.PhotoImage(image.copy())

            label.config(image=photo,text="")

            setattr(self,f"{kind}_preview_image",photo)

        except Exception as exc:

            label.config(image="",text=f"Could not preview image: {exc}")

            setattr(self,f"{kind}_preview_image",None)



    def load_greeting_preview(self, kind, image_id):

        if not image_id:return self.show_greeting_preview(kind)

        try:

            image=self.store.get_welcome_goodbye_image(image_id)

            if not image:return self.show_greeting_preview(kind,text="Saved image is no longer available")

            self.show_greeting_preview(kind,image[1]); getattr(self,f"{kind}_image_label").config(text="Saved image loaded from Cloudflare R2")

        except Exception as exc:

            self.show_greeting_preview(kind,text=f"Could not load saved image: {exc}")



    def choose_greeting_image(self, kind):

        path=filedialog.askopenfilename(title=f"Choose {kind} image",filetypes=[("Image files","*.png *.jpg *.jpeg *.webp *.gif"), ("All files","*.*")])

        if not path or not self.validate_image_upload(path):return

        try:

            with open(path,"rb") as image_file: self.show_greeting_preview(kind,image_file.read())

            if kind=="welcome": self.welcome_image_path=path; self.welcome_image_label.config(text=f"Selected: {path}")

            else: self.goodbye_image_path=path; self.goodbye_image_label.config(text=f"Selected: {path}")

        except Exception as exc: messagebox.showerror("Image error",f"Could not open this image: {exc}")



    def validate_image_upload(self, path):

        # Animated GIFs are uploaded byte-for-byte to R2 without a size limit.

        if path.lower().endswith(".gif"):

            return True

        if os.path.getsize(path) > MAX_IMAGE_UPLOAD_BYTES:

            messagebox.showwarning("Image too large", "Choose an image that is 10 MB or smaller.")

            return False

        return True



    def prepare_greeting_image(self, path):

        # GIFs are uploaded byte-for-byte: no size cap, frame reduction, or optimization.

        if path.lower().endswith(".gif"):

            with Image.open(path) as source:

                if (source.format or "").upper() != "GIF":

                    raise ValueError("This file is not a valid GIF.")

            with open(path, "rb") as image_file:

                return "image/gif", image_file.read()

        if os.path.getsize(path) > MAX_IMAGE_UPLOAD_BYTES:

            raise ValueError("Image must be 10 MB or smaller.")

        with Image.open(path) as source:

            # Preserve every GIF frame and force the Discord attachment to loop.

            # Previously this selected only frame 0 and saved a static JPEG.

            if (source.format or "").upper() == "GIF":

                durations, original_frames = [], []

                for frame in ImageSequence.Iterator(source):

                    durations.append(max(20, int(frame.info.get("duration", 100))))

                    original_frames.append(frame.convert("RGBA"))

                if not original_frames:

                    raise ValueError("The GIF has no animation frames.")

                # Very long GIFs cannot fit Cloudflare R2 even after resizing. Keep

                # the animation smooth while limiting the exported frame count.

                if len(original_frames) > 120:

                    compact_frames, compact_durations = [], []

                    step = len(original_frames) / 120

                    for index in range(120):

                        start = int(index * step)

                        end = max(start + 1, int((index + 1) * step))

                        compact_frames.append(original_frames[start])

                        compact_durations.append(sum(durations[start:end]))

                    original_frames, durations = compact_frames, compact_durations

                for maximum, colors in ((1920, 128), (1600, 96), (1280, 64), (1024, 48), (960, 32), (768, 24), (640, 16), (512, 12), (384, 8), (320, 8), (256, 8), (192, 6), (128, 4), (96, 4), (64, 2)):

                    frames = []

                    for frame in original_frames:

                        image = frame.copy()

                        image.thumbnail((min(maximum, 1920), min(maximum, 1080)), Image.Resampling.LANCZOS)

                        frames.append(image.convert("P", palette=Image.Palette.ADAPTIVE, colors=colors))

                    output = io.BytesIO()

                    frames[0].save(

                        output, format="GIF", save_all=True, append_images=frames[1:],

                        duration=durations, loop=0, disposal=2, optimize=True,

                    )

                    if output.tell() <= MAX_STORED_IMAGE_BYTES:

                        return "image/gif", output.getvalue()

                raise ValueError("This GIF is unusually long or complex. Please use a shorter GIF with fewer frames.")



            # Static images are stored compactly so encrypted Cloudflare R2 documents

            # remain below Cloudflare R2's approximately 1 MiB document limit.

            image = source.convert("RGBA") if source.mode in ("RGBA", "LA", "P") else source.convert("RGB")

            if image.mode == "RGBA":

                background = Image.new("RGB", image.size, "white"); background.paste(image, mask=image.getchannel("A")); image = background

            else:

                image = image.convert("RGB")

            image.thumbnail((1200, 675))

            for quality in (85, 75, 65, 55, 45, 35):

                output = io.BytesIO(); image.save(output, format="JPEG", quality=quality, optimize=True)

                if output.tell() <= MAX_STORED_IMAGE_BYTES:

                    return "image/jpeg", output.getvalue()

            while image.width > 320 and image.height > 180:

                image.thumbnail((max(320, int(image.width * .8)), max(180, int(image.height * .8))))

                output = io.BytesIO(); image.save(output, format="JPEG", quality=35, optimize=True)

                if output.tell() <= MAX_STORED_IMAGE_BYTES:

                    return "image/jpeg", output.getvalue()

        raise ValueError("This image could not be compressed for free Cloudflare R2 storage. Try a simpler image.")

    def save_greetings(self):

        server_index,welcome_index,goodbye_index=self.greeting_server.current(),self.welcome_channel.current(),self.goodbye_channel.current()

        if server_index<0 or welcome_index<0 or goodbye_index<0:

            return messagebox.showwarning("Greeting channels missing","Select a server plus both welcome and goodbye text channels.")

        try:

            guild_id=str(self.guilds[server_index][0]); channels=self.info.get(self.guilds[server_index][0],{}).get("text_channels",[])

            existing=self.settings.get("welcome_goodbye",{}).get(guild_id,{})

            config={"welcome_channel_id":channels[welcome_index]["id"],"goodbye_channel_id":channels[goodbye_index]["id"],

                    "welcome_message":self.welcome_message.get().strip(),"goodbye_message":self.goodbye_message.get().strip(),

                    "welcome_image_id":existing.get("welcome_image_id",""),"goodbye_image_id":existing.get("goodbye_image_id","")}

            for kind,path in (("welcome",self.welcome_image_path),("goodbye",self.goodbye_image_path)):

                if path:

                    content_type,data=self.prepare_greeting_image(path)

                    image_id=f"{guild_id}_{kind}"

                    self.store.save_welcome_goodbye_image(image_id,content_type,data); config[f"{kind}_image_id"]=image_id

            self.adopt_settings(self.store.save_welcome_goodbye_config(guild_id, config))

            if self.runtime:

                self.runtime.update_settings(self.settings)

            self.welcome_image_path=self.goodbye_image_path=None

            self.welcome_image_label.config(text="Saved to Cloudflare R2"); self.goodbye_image_label.config(text="Saved to Cloudflare R2")

            messagebox.showinfo("Saved","Welcome and goodbye settings/images are encrypted in Cloudflare R2. hosted worker synchronizes within 10 seconds.")

        except Exception as exc: messagebox.showerror("Greeting save error",str(exc))



    def refresh_counting_channels(self):

        i=self.counting_server.current()

        if i<0:return

        guild_id=self.guilds[i][0]; channels=self.info.get(guild_id,{}).get("text_channels",[])

        self.counting_channel["values"]=[f"# {channel['name']} ({channel['id']})" for channel in channels]

        configured=str(self.settings.get("counting_channels",{}).get(str(guild_id),""))

        selected=next((index for index,channel in enumerate(channels) if str(channel["id"])==configured),None)

        if selected is not None:self.counting_channel.current(selected)

        elif channels:self.counting_channel.current(0)



    def save_counting_channel(self):

        server_index,channel_index=self.counting_server.current(),self.counting_channel.current()

        if server_index<0 or channel_index<0:return messagebox.showwarning("Counting channel missing","Connect the GUI bot, then select a server and one text channel.")

        guild_id=self.guilds[server_index][0]; channels=self.info.get(guild_id,{}).get("text_channels",[])

        self.settings.setdefault("counting_channels",{})[str(guild_id)]=channels[channel_index]["id"]

        self.persist_settings()

        if self.runtime:self.runtime.update_settings(self.settings)

        messagebox.showinfo("Saved","Counting channel saved. The count starts at 1 in this channel; hosted worker synchronizes within 10 seconds.")

    def reset_counting_channel(self):

        server_index, channel_index = self.counting_server.current(), self.counting_channel.current()

        if server_index < 0 or channel_index < 0:

            return messagebox.showwarning("Counting channel missing", "Select the server and counting channel first.")

        if not self.runtime or not self.runtime.loop:

            return messagebox.showwarning("GUI bot offline", "Connect the GUI bot first to clear the Discord counting channel.")

        if not messagebox.askyesno("Reset and clear counting", "Delete ALL messages in this selected counting channel and restart the count at 1?\n\nThis cannot be undone."):

            return

        guild_id = self.guilds[server_index][0]

        channel_id = self.info.get(guild_id, {}).get("text_channels", [])[channel_index]["id"]

        self.submit_on_bot_loop(

            self.runtime.reset_counting_channel(guild_id, channel_id),

            "Counting channel cleared and the saved count reset to 1.",

        )

    def refresh_birthday_channels(self):

        i=self.birthday_server.current()

        if i<0:return

        guild_id=self.guilds[i][0]; channels=self.info.get(guild_id,{}).get("text_channels",[])

        values=[f"# {channel['name']} ({channel['id']})" for channel in channels]

        self.birthday_channel["values"]=values; self.birthday_wish_channel["values"]=values

        self.birthday_channel.set(""); self.birthday_wish_channel.set("")

        config=self.settings.get("birthdays",{}).get(str(guild_id),{})

        self.birthday_roles.delete(0,tk.END)

        for index,role in enumerate(self.info.get(guild_id,{}).get("roles",[])):

            self.birthday_roles.insert(tk.END,role["name"])

            if role["id"] in config.get("role_ids",[]): self.birthday_roles.selection_set(index)

        self.update_role_summary(self.birthday_roles, self.birthday_role_summary)

        saved=str(config.get("channel_id",""))

        for index,channel in enumerate(channels):

            if channel["id"]==saved: self.birthday_channel.current(index); break

        wish_saved=str(config.get("wish_channel_id") or saved)

        for index,channel in enumerate(channels):

            if channel["id"]==wish_saved: self.birthday_wish_channel.current(index); break

        self.load_birthday_preview(config.get("image_id",""))



    def show_birthday_preview(self, data=None, text=None):

        if not data:

            self.birthday_preview.config(image="",text=text or "No birthday image saved")

            self.birthday_preview_image=None

            return

        try:

            image=Image.open(io.BytesIO(data)); image.thumbnail((260,130)); photo=ImageTk.PhotoImage(image.copy())

            self.birthday_preview.config(image=photo,text=""); self.birthday_preview_image=photo

        except Exception as exc:

            self.birthday_preview.config(image="",text=f"Could not preview image: {exc}"); self.birthday_preview_image=None



    def load_birthday_preview(self, image_id):

        if not image_id: return self.show_birthday_preview()

        try:

            image=self.store.get_birthday_image(image_id)

            if not image: return self.show_birthday_preview(text="Saved image is no longer available")

            self.show_birthday_preview(image[1]); self.birthday_image_label.config(text="Saved image loaded from Cloudflare R2")

        except Exception as exc:

            self.show_birthday_preview(text=f"Could not load saved image: {exc}")



    def choose_birthday_image(self):

        path=filedialog.askopenfilename(title="Choose birthday image",filetypes=[("Image files","*.png *.jpg *.jpeg *.webp *.gif"), ("All files","*.*")])

        if not path or not self.validate_image_upload(path):return

        try:

            with open(path,"rb") as image_file: self.show_birthday_preview(image_file.read())

            self.birthday_image_path=path; self.birthday_image_label.config(text=f"Selected: {path}")

        except Exception as exc: messagebox.showerror("Image error",f"Could not open this image: {exc}")



    def save_birthday_channel(self):

        server_index,channel_index,wish_channel_index=self.birthday_server.current(),self.birthday_channel.current(),self.birthday_wish_channel.current()

        if server_index<0 or channel_index<0 or wish_channel_index<0: return messagebox.showwarning("Birthday channel missing","Connect the GUI bot, then select both birthday entry and wish text channels.")

        try:

            guild_id=str(self.guilds[server_index][0]); channels=self.info.get(self.guilds[server_index][0],{}).get("text_channels",[])

            existing=self.settings.get("birthdays",{}).get(guild_id,{})

            roles=self.info.get(self.guilds[server_index][0],{}).get("roles",[])

            config={"channel_id":channels[channel_index]["id"],"wish_channel_id":channels[wish_channel_index]["id"],"role_ids":[roles[index]["id"] for index in self.birthday_roles.curselection()],"image_id":existing.get("image_id",""), "timezone":self.local_timezone}

            if self.birthday_image_path:

                content_type,data=self.prepare_greeting_image(self.birthday_image_path)

                image_id=f"{guild_id}_birthday"

                self.store.save_birthday_image(image_id,content_type,data); config["image_id"]=image_id

            self.settings.setdefault("birthdays",{})[guild_id]=config; self.persist_settings()

            self.birthday_image_path=None; self.birthday_image_label.config(text="Saved to Cloudflare R2")

            messagebox.showinfo("Saved","Birthday settings/image are encrypted in Cloudflare R2. hosted worker synchronizes within 10 seconds.")

        except Exception as exc: messagebox.showerror("Birthday save error",str(exc))



    def load_tickets(self):

        try:

            self.tickets={ticket["document_id"]:ticket for ticket in self.store.list_tickets()}

            for item in self.ticket_tree.get_children(): self.ticket_tree.delete(item)

            for doc,ticket in self.tickets.items():

                category=ticket["category"] + (f" ({ticket['other_category']})" if ticket.get("other_category") else "")

                self.ticket_tree.insert("","end",iid=doc,values=(ticket["ticket_id"],ticket["status"],ticket["username"],ticket.get("assigned_moderator_id","-"),category,ticket.get("created_at","")))

            if self.tickets:

                first=next(iter(self.tickets)); self.ticket_tree.selection_set(first); self.show_ticket_detail()

            else:

                self.ticket_detail.config(state="normal"); self.ticket_detail.delete("1.0",tk.END)

                self.ticket_detail.insert(tk.END,"No tickets found yet. Users must click Create Ticket in Discord and submit the form.")

                self.ticket_detail.config(state="disabled")

        except Exception as exc: messagebox.showerror("Ticket error",str(exc))



    def auto_refresh_tickets(self):

        try: self.load_tickets()

        except Exception: pass

        self.root.after(15000,self.auto_refresh_tickets)



    def show_ticket_detail(self):

        selected=self.ticket_tree.selection()

        if not selected or selected[0] not in self.tickets:return

        ticket=self.tickets[selected[0]]

        category=ticket["category"] + (f" - {ticket['other_category']}" if ticket.get("other_category") else "")

        detail=(f"Ticket ID: #{ticket['ticket_id']}\nStatus: {ticket['status']}\nCreated: {ticket.get('created_at','-')}\n"

                f"User: {ticket['username']} (Discord ID: {ticket['user_id']})\n"

                f"Category: {category}\n\nProblem:\n{ticket['problem']}")

        if ticket.get("resolved_at"):

            action_label = "Closed" if ticket.get("status") == "closed" else "Resolved"

            detail += f"\n\n{action_label}: {ticket['resolved_at']}\n{action_label} by: {ticket.get('resolved_by','-')}\nNote sent to user: {ticket.get('resolution_note') or '-'}"

        self.ticket_detail.config(state="normal"); self.ticket_detail.delete("1.0",tk.END)

        self.ticket_detail.insert(tk.END,detail); self.ticket_detail.config(state="disabled")



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

        if ticket["status"] != "open": return messagebox.showinfo("Ticket closed",f"This ticket is already {ticket['status']}.")

        try: ticket=self.store.set_ticket_status(selected[0],"resolved","GUI operator",self.resolution.get().strip())

        except Exception as exc: return messagebox.showerror("Resolution error",str(exc))

        self.submit_on_bot_loop(self.runtime.finalize_ticket(ticket),f"Resolved ticket #{ticket['ticket_id']}, deleted its private chat, and sent user confirmation.")

        self.load_tickets()



    def close_ticket(self):

        selected=self.ticket_tree.selection()

        if not selected: return messagebox.showwarning("Ticket missing","Select a ticket first.")

        if not self.runtime or not self.runtime.loop: return messagebox.showwarning("GUI bot offline","Connect the GUI bot first to close and notify the user.")

        ticket=self.tickets[selected[0]]

        if ticket["status"] != "open": return messagebox.showinfo("Ticket closed",f"This ticket is already {ticket['status']}.")

        note=self.resolution.get().strip() or DEFAULT_TICKET_CLOSE_NOTE

        try: ticket=self.store.set_ticket_status(selected[0],"closed","GUI operator",note)

        except Exception as exc: return messagebox.showerror("Close error",str(exc))

        self.submit_on_bot_loop(self.runtime.finalize_ticket(ticket),f"Closed ticket #{ticket['ticket_id']}, deleted its private chat, and sent user confirmation.")

        self.load_tickets()



    def delete_ticket(self):

        selected=self.ticket_tree.selection()

        if not selected: return messagebox.showwarning("Ticket missing","Select a ticket first.")

        ticket=self.tickets.get(selected[0])

        if not ticket:return messagebox.showwarning("Ticket missing","The selected ticket is no longer available.")

        confirmed=messagebox.askyesno("Delete ticket permanently",f"Delete ticket #{ticket['ticket_id']} permanently from Cloudflare R2?\n\nThis cannot be undone.")

        if not confirmed:return

        try:

            self.store.delete_ticket(selected[0]); self.resolution.delete(0,tk.END); self.load_tickets()

            self.log(f"Deleted ticket #{ticket['ticket_id']} permanently from Cloudflare R2.")

            messagebox.showinfo("Deleted",f"Ticket #{ticket['ticket_id']} was deleted from Cloudflare R2.")

        except Exception as exc: messagebox.showerror("Delete error",str(exc))



    DEFAULT_WORD_BLOCKS = ["porn", "porno", "nude", "nudes", "onlyfans", "xxx", "sex video", "sexchat", "blowjob", "handjob", "dick pic", "pussy pic", "hentai", "nsfw", "camgirl", "escort", "free nitro", "nitro giveaway", "crypto giveaway", "click here"]



    def refresh_word_blocks(self):

        if not hasattr(self, "word_block_text") or self.word_block_server.current() < 0:

            return

        guild_id = str(self.guilds[self.word_block_server.current()][0])

        words = self.settings.get("word_blocks", {}).get(guild_id, self.DEFAULT_WORD_BLOCKS)

        self.word_block_text.delete("1.0", tk.END); self.word_block_text.insert("1.0", "\n".join(words))



    def save_word_blocks(self):

        if self.word_block_server.current() < 0:

            return messagebox.showwarning("Server missing", "Select a server first.")

        words = list(dict.fromkeys(word.strip().casefold() for word in self.word_block_text.get("1.0", tk.END).replace(",", "\n").splitlines() if word.strip()))

        if len(words) > 200:

            return messagebox.showwarning("Too many words", "Save at most 200 words or phrases.")

        guild_id = str(self.guilds[self.word_block_server.current()][0])

        self.settings.setdefault("word_blocks", {})[guild_id] = words

        self.persist_settings()

        if self.runtime: self.runtime.update_settings(self.settings)

        messagebox.showinfo("Saved", f"Saved {len(words)} blocked words. hosted worker synchronizes within 10 seconds.")



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

                    self.guilds=data; values=[f"{name} ({gid})" for gid,name in data]; self.custom_server["values"]=values; self.chat_server["values"]=values; self.link_server["values"]=values; self.drag_server["values"]=values; self.mod_guild["values"]=values; self.ticket_server["values"]=values; self.voice_server["values"]=values; self.greeting_server["values"]=values; self.level_server["values"]=values; self.counting_server["values"]=values; self.birthday_server["values"]=values; self.notif_server["values"]=values; self.movie_server["values"]=values; self.default_role_server["values"]=values; self.word_block_server["values"]=values

                    if data: self.custom_server.current(0); self.chat_server.current(0); self.link_server.current(0); self.drag_server.current(0); self.mod_guild.current(0); self.ticket_server.current(0); self.voice_server.current(0); self.greeting_server.current(0); self.level_server.current(0); self.counting_server.current(0); self.birthday_server.current(0); self.notif_server.current(0); self.movie_server.current(0); self.default_role_server.current(0)

                    self.set_status("Connected")

                elif kind=="guild": self.info[data["id"]]=data; self.refresh_custom_commands_ui(); self.show_chat_roles(); self.show_link_roles(); self.show_drag_roles(); self.refresh_ticket_channels(); self.refresh_voice_channels(); self.refresh_greeting_channels(); self.refresh_level_channels(); self.refresh_counting_channels(); self.refresh_birthday_channels(); self.refresh_notif_ui(); self.refresh_movie_ui(); self.refresh_default_role_ui(); self.refresh_word_blocks()

                elif kind=="ticket_moderator_verified":

                    user_id = data["id"]

                    if user_id in self.ticket_moderator_ids:

                        self.log(f"{data['display_name']} is already in the ticket staff list.")

                    else:

                        self.ticket_moderator_ids.append(user_id); self.ticket_moderator_details[user_id] = data

                        self.ticket_moderator_id.delete(0, tk.END); self.render_ticket_moderators()

                        self.log(f"Verified and added ticket staff: {data['display_name']} ({user_id}). Save moderator order to publish it.")

                elif kind=="auto_role_member_applied":

                    self.default_role_member_id.delete(0, tk.END)

                    status = "already had" if data.get("already_assigned") else "received"; self.log(f"Verified {data['display_name']} ({data['id']}); they {status} the saved Auto Role(s).")

                elif kind=="invite":

                    self.invite_url=data; self.invite_entry.config(state="normal"); self.invite_entry.delete(0,tk.END); self.invite_entry.insert(0,data); self.invite_entry.config(state="readonly")

                elif kind=="status": self.set_status(data)

                elif kind=="settings_sync":

                    self.settings_sync_inflight=False

                    if data.get("settings_revision",0)>self.settings.get("settings_revision",0):

                        self.settings=data; self.settings_baseline=copy.deepcopy(data)

                        if self.runtime: self.runtime.update_settings(data)

                        # Keep every settings tab aligned with Cloudflare R2, not only

                        # permission and Auto Role tabs. This is the same shared

                        # document hosted worker reads on its 10-second refresh cycle.

                        self.refresh_custom_commands_ui(); self.show_chat_roles(); self.show_link_roles(); self.show_drag_roles(); self.refresh_ticket_channels(); self.refresh_voice_channels(); self.refresh_greeting_channels(); self.refresh_level_channels(); self.refresh_counting_channels(); self.refresh_birthday_channels(); self.refresh_notif_ui(); self.refresh_movie_ui(); self.refresh_default_role_ui(); self.refresh_word_blocks()

                        self.header_subtitle.config(text="Settings synchronized from Cloudflare R2",fg=self.SUCCESS)

                elif kind=="settings_sync_error": self.settings_sync_inflight=False

                elif kind=="logs_cleared":

                    self.log(f"Deleted {data} Cloudflare R2 log(s) older than 30 days.")

                    self.load_logs()

                    messagebox.showinfo("Logs cleared", f"Deleted {data} Cloudflare R2 log(s) older than 30 days.")

                elif kind=="logs_clear_error": messagebox.showerror("Log cleanup error", data)

                elif kind=="db_usage":

                    d1_size,r2_size,approximate=data

                    label="D1 record estimate" if approximate else "D1 used"

                    self.database_status.config(text=f"{label}: {self.format_bytes(d1_size)}  |  R2 bot data used: {self.format_bytes(r2_size)}")

                    self.database_progress["value"]=100

                elif kind=="db_export_done":

                    path,records,objects=data

                    self.database_progress["value"]=100

                    self.database_status.config(text=f"Export complete: {records} D1 records, {objects} R2 objects.")

                    messagebox.showinfo("Backup exported",f"Complete JSON backup saved to:\n{path}\n\nKeep this file private; it contains all bot data and uploaded media.")

                elif kind=="db_import_done":

                    count,start=data

                    self.database_progress["value"]=100

                    self.database_status.config(text=f"Import complete: {count} items processed; resumed from item {start+1}.")

                    try:

                        self.adopt_settings(self.store.get_settings())

                        self.auto.set(bool(self.settings.get("auto_connect",True)))

                        self.refresh_custom_commands_ui(); self.show_chat_roles(); self.show_link_roles(); self.show_drag_roles(); self.refresh_ticket_channels(); self.refresh_voice_channels(); self.refresh_greeting_channels(); self.refresh_level_channels(); self.refresh_counting_channels(); self.refresh_birthday_channels(); self.refresh_notif_ui(); self.refresh_movie_ui(); self.refresh_default_role_ui(); self.refresh_word_blocks()

                    except Exception as exc:

                        self.log(f"Imported settings but could not refresh every GUI panel: {exc}")

                    messagebox.showinfo("Backup imported",f"Processed {count} backup items. Matching records and files were restored from the backup. The GUI has reloaded its settings; the connected bot should synchronize shortly.")

                elif kind=="db_progress":

                    percent,label=data

                    self.database_progress["value"]=percent

                    self.database_status.config(text=f"{label} — {percent}%")

                elif kind=="db_error":

                    self.database_progress["value"]=0

                    self.database_status.config(text="Database operation failed.")

                    messagebox.showerror("Database error",data)

                elif kind=="refresh_tickets": self.load_tickets()

                elif kind=="default_role_progress":

                    percent,done,total=data; self.default_role_progress["value"]=percent

                    self.default_role_progress_label.config(text=f"{percent}% ({done}/{total})" if total else "")

        except queue.Empty: pass

        self.root.after(200,self.poll)



if __name__=="__main__":

    root=tk.Tk(); App(root); root.mainloop()



