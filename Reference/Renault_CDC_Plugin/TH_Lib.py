from robot.api.deco import keyword
import tkinter as tk
import threading
import subprocess
import time
from datetime import datetime

class TH_Lib:
    def __init__(self):
        self.root = None
        self.label = None   # ✅ timestamp label 1개만 유지

    def _run_tk(self):
        self.root = tk.Tk()
        self.root.title("Panel")

        tkWidth = 300
        tkHeight = 300

        self.root.update_idletasks()

        x = 0
        y = self.root.winfo_screenheight() - tkHeight

        self.root.geometry(f"{tkWidth}x{tkHeight}+{x}+{y}")
        self.root.configure(bg='black')
        self.root.attributes('-topmost', True)

        self.root.mainloop()

    @keyword("Show TK Panel")
    def show_tk_panel(self):
        if self.root:
            self.root.after(0, self._reset_panel)
            return

        t = threading.Thread(target=self._run_tk, daemon=True)
        t.start()

    def _update_panel(self):
        if not self.root:
            return

        self.root.configure(bg='yellow')

        if self.label:
            self.label.destroy()

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

        self.label = tk.Label(
            self.root,
            text=now,
            bg='yellow',
            fg='black',
            font=("Arial", 8)  # ✅ 작게
        )

        self.label.pack(side='top', anchor='n', pady=2)

    def _reset_panel(self):
        if self.root:
            self.root.configure(bg='black')
        if self.label:
            self.label.destroy()
            self.label = None

    @keyword("Send Signal And Update Panel")
    def send_signal_and_update_panel(
        self,
        th_client_dir,
        topic_name,
        json_file,
        th_addr,
        python_bin
    ):
        cmd = [
            python_bin,
            "client.py",
            "--pub_topic_name", topic_name,
            "--json_path", json_file,
            "--ip_address", th_addr,
            "--log_level", "DEBUG"
        ]

        # start = time.perf_counter()

        process = subprocess.Popen(
            cmd,
            cwd=th_client_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        for line in iter(process.stdout.readline, ''):
            if "GEAR_LEVER_ACCEPTED_T_REVERSE" in line:
                # end = time.perf_counter()
                # print(f"E2E: {(end - start) * 1000:.3f} ms")
                if self.root:
                    self.root.after(0, self._update_panel)
                break
        process.wait()