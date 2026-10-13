
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import subprocess
import os
import json
import threading
import re


# ==========================================================
# DIRECT FFMPEG PATH
# ==========================================================

FFMPEG = r"C:\ffmpeg\bin\ffmpeg.exe"
FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe"


class VideoToGIF:

    def __init__(self, root):

        self.root = root

        self.root.title("Video → GIF Converter")
        self.root.geometry("700x600")
        self.root.resizable(False, False)

        self.video = ""
        self.output = ""

        self.video_duration = 0.0

        self.build_ui()
        self.check_ffmpeg()

    # ======================================================
    # UI
    # ======================================================

    def build_ui(self):

        tk.Label(
            self.root,
            text="VIDEO → GIF",
            font=("Segoe UI", 25, "bold")
        ).pack(pady=(20, 3))

        tk.Label(
            self.root,
            text="High Quality • Same FPS • Optimized Size",
            font=("Segoe UI", 10)
        ).pack(pady=(0, 18))

        # --------------------------------------------------
        # VIDEO
        # --------------------------------------------------

        video_frame = tk.Frame(self.root)

        video_frame.pack(
            fill="x",
            padx=35,
            pady=6
        )

        tk.Label(
            video_frame,
            text="Video",
            width=12,
            anchor="w",
            font=("Segoe UI", 10, "bold")
        ).pack(side="left")

        self.video_label = tk.Label(
            video_frame,
            text="No video selected",
            anchor="w"
        )

        self.video_label.pack(
            side="left",
            fill="x",
            expand=True
        )

        tk.Button(
            video_frame,
            text="Browse",
            width=11,
            command=self.select_video
        ).pack(side="right")

        # --------------------------------------------------
        # OUTPUT
        # --------------------------------------------------

        output_frame = tk.Frame(self.root)

        output_frame.pack(
            fill="x",
            padx=35,
            pady=6
        )

        tk.Label(
            output_frame,
            text="Output",
            width=12,
            anchor="w",
            font=("Segoe UI", 10, "bold")
        ).pack(side="left")

        self.output_label = tk.Label(
            output_frame,
            text="Automatic",
            anchor="w"
        )

        self.output_label.pack(
            side="left",
            fill="x",
            expand=True
        )

        tk.Button(
            output_frame,
            text="Save As",
            width=11,
            command=self.select_output
        ).pack(side="right")

        # --------------------------------------------------
        # SETTINGS
        # --------------------------------------------------

        settings = tk.LabelFrame(
            self.root,
            text=" GIF Settings ",
            padx=18,
            pady=12
        )

        settings.pack(
            fill="x",
            padx=35,
            pady=15
        )

        # FPS

        tk.Label(
            settings,
            text="FPS",
            width=15,
            anchor="w"
        ).grid(
            row=0,
            column=0,
            sticky="w",
            pady=5
        )

        self.fps_var = tk.StringVar(
            value="Source FPS"
        )

        self.fps_box = ttk.Combobox(
            settings,
            textvariable=self.fps_var,
            values=[
                "Source FPS",
                "60",
                "50",
                "30",
                "25",
                "24",
                "20",
                "15",
                "10"
            ],
            state="readonly",
            width=20
        )

        self.fps_box.grid(
            row=0,
            column=1,
            padx=20
        )

        # Resolution

        tk.Label(
            settings,
            text="Resolution",
            width=15,
            anchor="w"
        ).grid(
            row=1,
            column=0,
            sticky="w",
            pady=5
        )

        self.res_var = tk.StringVar(
            value="Original"
        )

        self.res_box = ttk.Combobox(
            settings,
            textvariable=self.res_var,
            values=[
                "Original",
                "1920",
                "1280",
                "1080",
                "720",
                "480",
                "360"
            ],
            state="readonly",
            width=20
        )

        self.res_box.grid(
            row=1,
            column=1,
            padx=20
        )

        # Quality

        tk.Label(
            settings,
            text="Quality",
            width=15,
            anchor="w"
        ).grid(
            row=2,
            column=0,
            sticky="w",
            pady=5
        )

        self.quality_var = tk.StringVar(
            value="High"
        )

        self.quality_box = ttk.Combobox(
            settings,
            textvariable=self.quality_var,
            values=[
                "Maximum",
                "High",
                "Balanced",
                "Small Size"
            ],
            state="readonly",
            width=20
        )

        self.quality_box.grid(
            row=2,
            column=1,
            padx=20
        )

        # Dithering

        tk.Label(
            settings,
            text="Dithering",
            width=15,
            anchor="w"
        ).grid(
            row=3,
            column=0,
            sticky="w",
            pady=5
        )

        self.dither_var = tk.StringVar(
            value="Sierra2_4a"
        )

        self.dither_box = ttk.Combobox(
            settings,
            textvariable=self.dither_var,
            values=[
                "Sierra2_4a",
                "FloydSteinberg",
                "Bayer",
                "None"
            ],
            state="readonly",
            width=20
        )

        self.dither_box.grid(
            row=3,
            column=1,
            padx=20
        )

        # --------------------------------------------------
        # VIDEO INFO
        # --------------------------------------------------

        self.info_label = tk.Label(
            self.root,
            text="No video loaded",
            font=("Segoe UI", 9)
        )

        self.info_label.pack(
            pady=(2, 5)
        )

        # --------------------------------------------------
        # PROGRESS
        # --------------------------------------------------

        self.progress = ttk.Progressbar(
            self.root,
            orient="horizontal",
            length=620,
            mode="determinate",
            maximum=100
        )

        self.progress.pack(
            pady=(5, 5)
        )

        self.percent_label = tk.Label(
            self.root,
            text="0%",
            font=("Segoe UI", 10, "bold")
        )

        self.percent_label.pack()

        self.status = tk.Label(
            self.root,
            text="Checking FFmpeg...",
            font=("Segoe UI", 10)
        )

        self.status.pack(
            pady=3
        )

        # --------------------------------------------------
        # START
        # --------------------------------------------------

        self.start_button = tk.Button(
            self.root,
            text="▶  START CONVERSION",
            font=("Segoe UI", 13, "bold"),
            width=30,
            height=2,
            command=self.start_conversion
        )

        self.start_button.pack(
            pady=15
        )

    # ======================================================
    # FFMPEG CHECK
    # ======================================================

    def check_ffmpeg(self):

        if not os.path.isfile(FFMPEG):

            self.start_button.config(
                state="disabled"
            )

            self.status.config(
                text="✕ FFmpeg not found"
            )

            messagebox.showerror(
                "FFmpeg Not Found",
                "FFmpeg পাওয়া যায়নি.\n\n"
                "Expected location:\n\n"
                f"{FFMPEG}\n\n"
                "FFmpeg folder structure check করো."
            )

            return

        if not os.path.isfile(FFPROBE):

            self.start_button.config(
                state="disabled"
            )

            self.status.config(
                text="✕ FFprobe not found"
            )

            messagebox.showerror(
                "FFprobe Not Found",
                "FFprobe পাওয়া যায়নি.\n\n"
                "Expected location:\n\n"
                f"{FFPROBE}"
            )

            return

        self.status.config(
            text="✓ FFmpeg ready"
        )

    # ======================================================
    # SELECT VIDEO
    # ======================================================

    def select_video(self):

        path = filedialog.askopenfilename(
            title="Select Video",
            filetypes=[
                (
                    "Video Files",
                    "*.mp4 *.mkv *.avi *.mov "
                    "*.webm *.flv *.wmv"
                ),
                (
                    "All Files",
                    "*.*"
                )
            ]
        )

        if not path:
            return

        self.video = path

        filename = os.path.basename(path)

        if len(filename) > 52:

            filename = (
                filename[:49] +
                "..."
            )

        self.video_label.config(
            text=filename
        )

        base = os.path.splitext(path)[0]

        self.output = base + ".gif"

        self.output_label.config(
            text=os.path.basename(
                self.output
            )
        )

        self.status.config(
            text="Reading video information..."
        )

        threading.Thread(
            target=self.load_video_info,
            daemon=True
        ).start()

    # ======================================================
    # LOAD VIDEO INFO
    # ======================================================

    def load_video_info(self):

        try:

            width, height, fps, duration = (
                self.get_video_info()
            )

            self.video_duration = duration

            duration_text = self.format_time(
                duration
            )

            text = (
                f"{width} × {height}   •   "
                f"{fps:.2f} FPS   •   "
                f"{duration_text}"
            )

            self.root.after(
                0,
                lambda msg=text: (
                    self.info_label.config(
                        text=msg
                    ),
                    self.status.config(
                        text="✓ Video ready"
                    )
                )
            )

        except Exception as exc:

            error_text = str(exc)

            self.root.after(
                0,
                lambda msg=error_text: (
                    self.status.config(
                        text="✕ Could not read video"
                    ),
                    messagebox.showerror(
                        "Video Error",
                        msg
                    )
                )
            )

    # ======================================================
    # GET VIDEO INFO
    # ======================================================

    def get_video_info(self):

        command = [
            FFPROBE,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            self.video
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )

        if result.returncode != 0:

            raise RuntimeError(
                result.stderr.strip() or
                "FFprobe failed."
            )

        try:

            data = json.loads(
                result.stdout
            )

        except json.JSONDecodeError:

            raise RuntimeError(
                "Could not parse FFprobe output."
            )

        stream = None

        for item in data.get(
            "streams",
            []
        ):

            if item.get(
                "codec_type"
            ) == "video":

                stream = item
                break

        if stream is None:

            raise RuntimeError(
                "No video stream found."
            )

        width = int(
            stream["width"]
        )

        height = int(
            stream["height"]
        )

        fps_text = stream.get(
            "r_frame_rate",
            "30/1"
        )

        try:

            num, den = (
                fps_text.split("/")
            )

            num = float(num)
            den = float(den)

            fps = (
                num / den
                if den != 0
                else 30.0
            )

        except Exception:

            fps = 30.0

        duration = 0.0

        try:

            duration = float(
                stream.get(
                    "duration",
                    0
                ) or 0
            )

        except Exception:

            try:

                duration = float(
                    data.get(
                        "format",
                        {}
                    ).get(
                        "duration",
                        0
                    ) or 0
                )

            except Exception:

                duration = 0.0

        return (
            width,
            height,
            fps,
            duration
        )

    # ======================================================
    # SELECT OUTPUT
    # ======================================================

    def select_output(self):

        path = filedialog.asksaveasfilename(
            title="Save GIF",
            defaultextension=".gif",
            filetypes=[
                (
                    "GIF File",
                    "*.gif"
                )
            ]
        )

        if not path:
            return

        self.output = path

        self.output_label.config(
            text=os.path.basename(
                path
            )
        )

    # ======================================================
    # START
    # ======================================================

    def start_conversion(self):

        if not os.path.isfile(FFMPEG):

            messagebox.showerror(
                "Error",
                f"FFmpeg not found:\n\n{FFMPEG}"
            )

            return

        if not os.path.isfile(FFPROBE):

            messagebox.showerror(
                "Error",
                f"FFprobe not found:\n\n{FFPROBE}"
            )

            return

        if not self.video:

            messagebox.showwarning(
                "No Video",
                "Please select a video first."
            )

            return

        if not os.path.isfile(
            self.video
        ):

            messagebox.showerror(
                "Error",
                "Selected video does not exist."
            )

            return

        if not self.output:

            base = os.path.splitext(
                self.video
            )[0]

            self.output = (
                base +
                ".gif"
            )

        self.start_button.config(
            state="disabled",
            text="⏳  CONVERTING..."
        )

        self.progress["value"] = 0

        self.percent_label.config(
            text="0%"
        )

        self.status.config(
            text="Starting FFmpeg..."
        )

        threading.Thread(
            target=self.convert,
            daemon=True
        ).start()

    # ======================================================
    # CONVERT
    # ======================================================

    def convert(self):

        try:

            width, height, source_fps, duration = (
                self.get_video_info()
            )

            # ------------------------------------------------
            # FPS
            # ------------------------------------------------

            fps_setting = (
                self.fps_var.get()
            )

            if fps_setting == "Source FPS":

                fps = source_fps

            else:

                fps = float(
                    fps_setting
                )

            # ------------------------------------------------
            # RESOLUTION
            # ------------------------------------------------

            resolution = (
                self.res_var.get()
            )

            if resolution == "Original":

                target_width = width
                target_height = height

            else:

                target_width = int(
                    resolution
                )

                target_height = int(
                    height *
                    target_width /
                    width
                )

            # Even dimensions

            target_width -= (
                target_width % 2
            )

            target_height -= (
                target_height % 2
            )

            target_width = max(
                2,
                target_width
            )

            target_height = max(
                2,
                target_height
            )

            # ------------------------------------------------
            # QUALITY
            # ------------------------------------------------

            quality = (
                self.quality_var.get()
            )

            if quality == "Maximum":

                colors = 256
                stats = "full"

            elif quality == "High":

                colors = 256
                stats = "diff"

            elif quality == "Balanced":

                colors = 192
                stats = "diff"

            else:

                colors = 128
                stats = "diff"

            # ------------------------------------------------
            # DITHER
            # ------------------------------------------------

            dither = (
                self.dither_var.get()
            )

            # ------------------------------------------------
            # FILTER
            # ------------------------------------------------

            filter_complex = (
                f"fps={fps},"
                f"scale={target_width}:"
                f"{target_height}:"
                f"flags=lanczos,"
                f"split[s0][s1];"
                f"[s0]"
                f"palettegen="
                f"max_colors={colors}:"
                f"stats_mode={stats}"
                f"[palette];"
                f"[s1][palette]"
                f"paletteuse="
                f"dither={dither}"
            )

            command = [
                FFMPEG,
                "-y",
                "-i",
                self.video,
                "-filter_complex",
                filter_complex,
                "-loop",
                "0",
                self.output
            ]

            # ------------------------------------------------
            # START PROCESS
            # ------------------------------------------------

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1
            )

            last_percent = -1

            # ------------------------------------------------
            # READ FFMPEG OUTPUT
            # ------------------------------------------------

            for line in process.stdout:

                line = line.strip()

                # FFmpeg outputs time like:
                # time=00:00:05.23

                match = re.search(
                    r"time=(\d+):(\d+):(\d+(?:\.\d+)?)",
                    line
                )

                if match and duration > 0:

                    hours = float(
                        match.group(1)
                    )

                    minutes = float(
                        match.group(2)
                    )

                    seconds = float(
                        match.group(3)
                    )

                    current = (
                        hours * 3600 +
                        minutes * 60 +
                        seconds
                    )

                    percent = int(
                        min(
                            100,
                            (
                                current /
                                duration
                            ) * 100
                        )
                    )

                    if percent != last_percent:

                        last_percent = percent

                        self.root.after(
                            0,
                            lambda p=percent: (
                                self.progress.configure(
                                    value=p
                                ),
                                self.percent_label.config(
                                    text=f"{p}%"
                                ),
                                self.status.config(
                                    text=(
                                        f"Converting... {p}%"
                                    )
                                )
                            )
                        )

            process.wait()

            if process.returncode != 0:

                raise RuntimeError(
                    "FFmpeg conversion failed.\n\n"
                    f"Exit code: "
                    f"{process.returncode}"
                )

            self.root.after(
                0,
                self.finished
            )

        except Exception as exc:

            error_text = str(exc)

            self.root.after(
                0,
                lambda msg=error_text: (
                    self.failed(msg)
                )
            )

    # ======================================================
    # FINISHED
    # ======================================================

    def finished(self):

        self.progress["value"] = 100

        self.percent_label.config(
            text="100%"
        )

        self.start_button.config(
            state="normal",
            text="▶  START CONVERSION"
        )

        self.status.config(
            text="✓ Conversion completed!"
        )

        try:

            file_size = os.path.getsize(
                self.output
            )

            if file_size >= 1024 * 1024:

                size = (
                    f"{file_size / (1024 * 1024):.2f} MB"
                )

            elif file_size >= 1024:

                size = (
                    f"{file_size / 1024:.1f} KB"
                )

            else:

                size = (
                    f"{file_size} Bytes"
                )

        except Exception:

            size = "Unknown"

        messagebox.showinfo(
            "Conversion Complete",
            "GIF successfully created!\n\n"
            f"File:\n{self.output}\n\n"
            f"Size: {size}"
        )

    # ======================================================
    # FAILED
    # ======================================================

    def failed(self, error_message):

        self.progress["value"] = 0

        self.percent_label.config(
            text="0%"
        )

        self.start_button.config(
            state="normal",
            text="▶  START CONVERSION"
        )

        self.status.config(
            text="✕ Conversion failed."
        )

        messagebox.showerror(
            "Conversion Error",
            error_message
        )

    # ======================================================
    # TIME FORMAT
    # ======================================================

    @staticmethod
    def format_time(seconds):

        try:

            seconds = int(
                float(seconds)
            )

        except Exception:

            return "Unknown"

        hours = seconds // 3600

        minutes = (
            seconds % 3600
        ) // 60

        secs = (
            seconds % 60
        )

        if hours > 0:

            return (
                f"{hours:02d}:"
                f"{minutes:02d}:"
                f"{secs:02d}"
            )

        return (
            f"{minutes:02d}:"
            f"{secs:02d}"
        )


# ==========================================================
# MAIN
# ==========================================================

if __name__ == "__main__":

    root = tk.Tk()

    app = VideoToGIF(root)

    root.mainloop()