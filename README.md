# Video Sorter (human-in-the-loop)

Lightweight desktop tool to watch each clip, press a hotkey, and automatically move/copy the file into the right folder while logging your decisions.

## Setup
- Python 3.10+ recommended.
- Install deps: `python -m pip install -r requirements.txt`
- Place raw videos in `videos_to_label/` (created automatically). Supported: mp4, mov, avi, mkv, mpg, mpeg, wmv.
- Label mapping lives in `config/labels.json`. Edit keys/names/dest folders as you like.

## Running
```
python main.py                # uses videos_to_label/, config/labels.json, logs/labels.csv, move mode
python main.py --pick-source  # opens a folder picker for the source directory
python main.py --mode copy    # copy instead of move
python main.py --sorted-root D:\sorted_output  # base for relative dest paths
```

## Controls
- Press the configured key (e.g., `1`, `2`, `3`, `4`) to label -> file moves/copies to its destination.
- `Space` play/pause, `Backspace` undo last action, `S` skip without moving.
- Playback bar at bottom: drag/seek, see elapsed/total time.
- Speed button cycles playback rate (0.5x, 1.0x, 1.5x, 2.0x, 4.0x, 6.0x, 8.0x, 10.0x).
- On-screen legend shows the active key mapping.

## Logging, resume, undo
- Actions are appended to `logs/labels.csv` (timestamp, key, label, original path, destination, action).
- On startup, files already listed in the log are skipped so you can resume where you left off.
- Undo rewinds the last move/copy: it moves the file back, removes the last log row, and reloads the clip.

## Config format (`config/labels.json`)
```json
[
  { "key": "1", "name": "True Trespasser", "dest": "True Trespasser" },
  { "key": "2", "name": "Yard/Station",   "dest": "Yard Station" },
  { "key": "3", "name": "Not Sure",       "dest": "Not Sure" },
  { "key": "4", "name": "No Detections",  "dest": "No Detections" }
]
```
- `key`: keyboard shortcut.
- `name`: label recorded in the log.
- `dest`: folder under `--sorted-root` (relative) or an absolute path. Folders are created if missing.

## Notes
- Create Folders named, "True Trespasser","Yard Station","Not Sure" and "No Detections" under a "sorted" folder as destinations for validated files, if not already created.
- Default sorted root is `<source_parent>/sorted`; change with `--sorted-root`. Pre-created destinations: `sorted/True Trespasser`, `sorted/Yard Station`, `sorted/Not Sure`, `sorted/No Detections`.
- If a destination already has the same filename, it auto-renames with `__001`, `__002`, ...
- For reliable playback, ensure the OS has codecs for your video formats (PySide6 uses the system media stack on Windows).
