import os
import tkinter as tk
from tkinter import filedialog, messagebox

BASE_DIR = "/home/cdc/Desktop/TH"
OUT_FILE = "/tmp/th_home.txt"

def select_th():
    path = filedialog.askdirectory(
        initialdir=BASE_DIR,
        title="Select TH version directory"
    )

    if not path:
        messagebox.showerror("Error", "TH directory not selected")
        root.quit()
        return

    if not os.path.isfile(os.path.join(path, "bin/launch_cvd")):
        messagebox.showerror("Error", "Invalid TH directory")
        root.quit()
        return

    with open(OUT_FILE, "w") as f:
        f.write(path)

    messagebox.showinfo("Selected", f"TH selected:\n{path}")
    root.quit()

root = tk.Tk()
root.title("TH Version Selector")
root.geometry("400x120")

tk.Label(root, text="Select Test Harness directory").pack(pady=10)
tk.Button(root, text="Browse", width=20, command=select_th).pack()

root.mainloop()
