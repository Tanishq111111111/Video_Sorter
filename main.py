import argparse
import csv
import json
import shutil
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Slot
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
    QSlider,
)


SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".mpg", ".mpeg", ".wmv"}


def load_labels(config_path: Path, sorted_root: Path):
    with config_path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    labels = []
    for item in data:
        key = str(item["key"])
        name = item["name"]
        dest = Path(item["dest"])
        if not dest.is_absolute():
            dest = sorted_root / dest
        labels.append({"key": key, "name": name, "dest": dest})
    return labels


def ensure_unique_path(dest_path: Path) -> Path:
    if not dest_path.exists():
        return dest_path
    stem = dest_path.stem
    suffix = dest_path.suffix
    parent = dest_path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}__{counter:03d}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def read_logged_paths(log_path: Path) -> set[Path]:
    if not log_path.exists():
        return set()
    seen = set()
    with log_path.open("r", newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            if row.get("original_path"):
                seen.add(Path(row["original_path"]))
    return seen


class VideoLabeler(QMainWindow):
    def __init__(
        self,
        source_dir: Path,
        labels: list[dict],
        log_path: Path,
        move_mode: str,
    ):
        super().__init__()
        self.setWindowTitle("Video Sorter — human-in-the-loop labeling")
        self.source_dir = source_dir
        self.labels = labels
        self.log_path = log_path
        self.move_mode = move_mode  # "move" or "copy"
        self.undo_stack: list[tuple[Path, Path]] = []
        self.scrubbing = False
        self.duration_ms = 0
        self.speed_steps = [0.5, 1.0, 1.5, 2.0, 4.0, 6.0, 8.0, 10.0]
        self.speed_index = 1

        self.queue = self.build_queue()
        self.current_path: Path | None = None

        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.video_widget = QVideoWidget()
        self.player.setVideoOutput(self.video_widget)

        self.status_label = QLabel("Ready")
        self.progress_label = QLabel("")
        self.time_label = QLabel("00:00 / 00:00")

        self.label_list = QListWidget()
        self.label_list.setFocusPolicy(Qt.NoFocus)
        for label in self.labels:
            item = QListWidgetItem(f"{label['key']}: {label['name']}")
            self.label_list.addItem(item)

        self.play_pause_btn = QPushButton("Pause")
        self.play_pause_btn.clicked.connect(self.toggle_playback)
        self.speed_btn = QPushButton("1.0x")
        self.speed_btn.clicked.connect(self.cycle_speed)

        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderPressed.connect(self.on_slider_pressed)
        self.seek_slider.sliderReleased.connect(self.on_slider_released)
        self.seek_slider.sliderMoved.connect(self.on_slider_moved)

        layout = QHBoxLayout()
        left_col = QVBoxLayout()
        left_col.addWidget(self.video_widget, stretch=10)

        bottom_controls = QHBoxLayout()
        bottom_controls.addWidget(self.play_pause_btn)
        bottom_controls.addWidget(self.speed_btn)
        bottom_controls.addWidget(self.seek_slider, stretch=10)
        bottom_controls.addWidget(self.time_label)
        left_col.addLayout(bottom_controls)

        left_widget = QWidget()
        left_widget.setLayout(left_col)

        right = QVBoxLayout()
        right.addWidget(QLabel("Hotkeys"))
        right.addWidget(self.label_list)
        right.addWidget(QLabel("Backspace = Undo\nS = Skip\nSpace = Play/Pause"))
        right.addStretch()
        right.addWidget(self.status_label)
        right.addWidget(self.progress_label)

        layout.addWidget(left_widget, stretch=3)
        right_widget = QWidget()
        right_widget.setLayout(right)
        layout.addWidget(right_widget, stretch=1)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self.build_shortcuts()
        self.player.mediaStatusChanged.connect(self.handle_media_status)
        self.player.positionChanged.connect(self.update_position)
        self.player.durationChanged.connect(self.update_duration)

        self.ensure_log_header()
        self.load_next()

    def release_media(self):
        """Stop playback and release file handles so moves don't fail on Windows."""
        self.player.stop()
        self.player.setSource(QUrl())  # clear to drop OS handle
        self.play_pause_btn.setText("Play")
        self.seek_slider.setValue(0)
        self.time_label.setText("00:00 / 00:00")
        self.duration_ms = 0
        self.scrubbing = False

    def build_shortcuts(self):
        for label in self.labels:
            shortcut = QShortcut(QKeySequence(label["key"]), self)
            shortcut.activated.connect(lambda l=label: self.label_current(l))
        skip = QShortcut(QKeySequence("S"), self)
        skip.activated.connect(self.skip_current)
        undo = QShortcut(QKeySequence(Qt.Key_Backspace), self)
        undo.activated.connect(self.undo_last)
        pause = QShortcut(QKeySequence(Qt.Key_Space), self)
        pause.activated.connect(self.toggle_playback)

    def build_queue(self) -> deque[Path]:
        logged = read_logged_paths(self.log_path)
        paths = []
        for path in sorted(self.source_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                if path in logged:
                    continue
                paths.append(path)
        return deque(paths)

    def ensure_log_header(self):
        if self.log_path.exists():
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.writer(fp)
            writer.writerow(
                [
                    "timestamp",
                    "key",
                    "label",
                    "original_path",
                    "dest_path",
                    "action",
                ]
            )

    def log_action(self, key: str, label: str, original: Path, dest: Path, action: str):
        with self.log_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.writer(fp)
            writer.writerow(
                [
                    datetime.utcnow().isoformat(),
                    key,
                    label,
                    str(original),
                    str(dest),
                    action,
                ]
            )

    def load_next(self):
        if not self.queue:
            self.current_path = None
            self.status_label.setText("All videos labeled.")
            self.progress_label.setText("")
            self.player.stop()
            return
        self.current_path = self.queue.popleft()
        self.status_label.setText(f"Loaded: {self.current_path.name}")
        remaining = len(self.queue)
        self.progress_label.setText(f"{remaining} remaining")
        self.player.setSource(QUrl.fromLocalFile(str(self.current_path)))
        self.player.play()
        self.play_pause_btn.setText("Pause")

    @Slot()
    def toggle_playback(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_pause_btn.setText("Play")
        else:
            self.player.play()
            self.play_pause_btn.setText("Pause")

    @Slot()
    def cycle_speed(self):
        self.speed_index = (self.speed_index + 1) % len(self.speed_steps)
        rate = self.speed_steps[self.speed_index]
        self.player.setPlaybackRate(rate)
        self.speed_btn.setText(f"{rate:.1f}x")

    @Slot()
    def on_slider_pressed(self):
        self.scrubbing = True

    @Slot()
    def on_slider_released(self):
        if self.duration_ms > 0:
            self.player.setPosition(self.seek_slider.value())
        self.scrubbing = False

    @Slot()
    def on_slider_moved(self, value: int):
        # Update current time display while scrubbing.
        self.time_label.setText(f"{self.format_ms(value)} / {self.format_ms(self.duration_ms)}")

    @Slot()
    def update_position(self, position: int):
        if not self.scrubbing:
            self.seek_slider.setValue(position)
            self.time_label.setText(f"{self.format_ms(position)} / {self.format_ms(self.duration_ms)}")

    @Slot()
    def update_duration(self, duration: int):
        self.duration_ms = duration
        self.seek_slider.setRange(0, duration if duration > 0 else 0)
        self.time_label.setText(f"{self.format_ms(0)} / {self.format_ms(duration)}")

    def format_ms(self, ms: int) -> str:
        total_seconds = ms // 1000
        mins = total_seconds // 60
        secs = total_seconds % 60
        return f"{mins:02d}:{secs:02d}"

    @Slot()
    def skip_current(self):
        if not self.current_path:
            return
        self.release_media()
        self.log_action("", "skip", self.current_path, self.current_path, "skip")
        self.load_next()

    @Slot()
    def label_current(self, label: dict):
        if not self.current_path:
            return
        # Release file handle before moving to avoid WinError 32
        self.release_media()
        dest_dir = label["dest"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = ensure_unique_path(dest_dir / self.current_path.name)
        try:
            if self.move_mode == "copy":
                shutil.copy2(self.current_path, target)
            else:
                shutil.move(self.current_path, target)
        except PermissionError as exc:
            QMessageBox.warning(
                self,
                "Move failed",
                f"Could not move file (is it open elsewhere?):\n{self.current_path}\n\n{exc}",
            )
            # Re-attach the media so user can retry or skip
            self.player.setSource(QUrl.fromLocalFile(str(self.current_path)))
            self.player.play()
            self.play_pause_btn.setText("Pause")
            return
        self.undo_stack.append((self.current_path, target))
        self.log_action(label["key"], label["name"], self.current_path, target, self.move_mode)
        self.load_next()

    @Slot()
    def undo_last(self):
        if not self.undo_stack:
            self.status_label.setText("Nothing to undo.")
            return
        self.release_media()
        original, dest = self.undo_stack.pop()
        if dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(dest, original)
        self.remove_last_log_entry()
        self.queue.appendleft(original)
        self.status_label.setText(f"Undid: {original.name}")
        self.progress_label.setText(f"{len(self.queue)} remaining")
        self.player.setSource(QUrl.fromLocalFile(str(original)))
        self.player.play()

    def remove_last_log_entry(self):
        if not self.log_path.exists():
            return
        with self.log_path.open("r", newline="", encoding="utf-8") as fp:
            rows = list(csv.reader(fp))
        if len(rows) <= 1:
            return
        rows = rows[:-1]
        with self.log_path.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.writer(fp)
            writer.writerows(rows)

    @Slot()
    def handle_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            QMessageBox.warning(self, "Playback error", "Cannot play this file.")
            self.load_next()


def select_source_dir(default: Path) -> Path:
    dialog = QFileDialog()
    dialog.setFileMode(QFileDialog.Directory)
    dialog.setOption(QFileDialog.ShowDirsOnly, True)
    if dialog.exec():
        dirs = dialog.selectedFiles()
        if dirs:
            return Path(dirs[0])
    return default


def parse_args():
    parser = argparse.ArgumentParser(description="Label and sort videos quickly.")
    parser.add_argument(
        "--source",
        "-s",
        type=Path,
        default=Path("videos_to_label"),
        help="Folder containing videos to label.",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=Path("config/labels.json"),
        help="JSON file mapping keys to label destinations.",
    )
    parser.add_argument(
        "--log",
        "-l",
        type=Path,
        default=Path("logs/labels.csv"),
        help="CSV log path.",
    )
    parser.add_argument(
        "--mode",
        choices=["move", "copy"],
        default="move",
        help="Move files (default) or copy them when labeling.",
    )
    parser.add_argument(
        "--sorted-root",
        type=Path,
        default=None,
        help="Root folder for relative dest paths. Defaults to source parent.",
    )
    parser.add_argument(
        "--pick-source",
        action="store_true",
        help="Open a folder picker for the source directory on launch.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    source_dir = args.source
    if args.pick_source:
        app = QApplication(sys.argv)
        source_dir = select_source_dir(source_dir)
        if not source_dir.exists():
            print("No source folder selected.", file=sys.stderr)
            return 1
    else:
        app = QApplication(sys.argv)

    source_dir.mkdir(parents=True, exist_ok=True)
    sorted_root = args.sorted_root or source_dir.parent / "sorted"
    labels = load_labels(args.config, sorted_root)

    window = VideoLabeler(
        source_dir=source_dir,
        labels=labels,
        log_path=args.log,
        move_mode=args.mode,
    )
    window.resize(1280, 720)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
