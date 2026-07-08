import sys
sys.stdout.reconfigure(line_buffering=True)

print("step A: before main imports")
import json, time, threading
from pathlib import Path
import uiautomation as auto
import keyboard, psutil, pyperclip
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
print("step B: imports done")

# Simulate module-level keyboard hook
def noop(e): pass
keyboard.hook(noop)
print("step C: keyboard hook done")

# Simulate _start_file_watcher
import string, ctypes
observer = Observer()
class _H(FileSystemEventHandler):
    def on_modified(self, e): pass
bitmask = ctypes.windll.kernel32.GetLogicalDrives()
for letter in string.ascii_uppercase:
    if bitmask & 1:
        p = Path(letter + ":\\")
        if p.exists():
            observer.schedule(_H(), str(p), recursive=True)
            print("  watching", p)
    bitmask >>= 1
observer.start()
print("step D: watcher started")

# Simulate _start_tray
try:
    import pystray
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0,0,0,0))
    icon = pystray.Icon("test", img, "test", menu=pystray.Menu())
    threading.Thread(target=icon.run, daemon=True).start()
    print("step E: tray started")
except Exception as e:
    print("step E: tray failed:", e)

print("step F: entering main loop")
time.sleep(3)
print("step G: done")
observer.stop()
