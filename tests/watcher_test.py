import time, string, ctypes
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class _SaveHandler(FileSystemEventHandler):
    def on_modified(self, event): pass

print('Starting observer...')
observer = Observer()
handler = _SaveHandler()

bitmask = ctypes.windll.kernel32.GetLogicalDrives()
drives = []
for letter in string.ascii_uppercase:
    if bitmask & 1:
        p = Path(letter + ":\\")
        if p.exists():
            drives.append(p)
    bitmask >>= 1

print("Found drives:", drives)
for drive in drives:
    print("Scheduling", drive)
    observer.schedule(handler, str(drive), recursive=True)
    print("  ok")

print("Starting observer thread...")
observer.start()
print("Observer started OK")
time.sleep(2)
observer.stop()
print("Done")
