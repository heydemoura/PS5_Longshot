"""Longshot PS5 Builder, the window: choose your game file, build the PS5 title folder, copy it to the console.

    python app.py

PyQt6. The build itself is ps5/builder/pipeline.py, run as a process of its own with this Python; this window shows its
steps and its log, and can send the finished folder to a console over FTP. Nothing is sent anywhere else.
"""
import ftplib
import os
import signal
import subprocess
import sys

from PyQt6.QtCore import QSettings, Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont, QIcon, QTextCursor
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QVBoxLayout, QWidget)

HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINE = os.environ.get("LONGSHOT_PS5_PIPELINE", os.path.join(HERE, "pipeline.py"))
STEPS = ["Reading the game's program", "Translating it to C", "Compiling for the console", "Linking", "Making the title folder",
         "Listing the game's shaders"]
ABOUT = {
    1: "A few seconds.",
    2: "5 to 15 minutes. About 2 GB of memory, more to go faster where it is free.",
    3: "The long step: 30 to 60 minutes on a 6-core PC; 1 to 2 hours with Memory to use at 2 or 3 GB.",
    4: "Under a minute.",
    5: "A few seconds.",
    6: "A minute or two.",
}


def python_exe():
    """the Python the build runs with: this one; on Windows python.exe beside pythonw.exe, which the window runs with"""
    exe = sys.executable
    if os.name == "nt" and os.path.basename(exe).lower() == "pythonw.exe":
        console = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(console):
            return console
    return exe


class Build(QThread):
    """pipeline.py as a process of its own, its lines as they come. On Windows it gets a console of its own, hidden, which
    the compilers it starts share: none of them opens a window. Stopping it stops what it started too."""
    line = pyqtSignal(str)
    ended = pyqtSignal(int)

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.proc = None
        self.failed_to_start = ""

    def run(self):
        kw = {}
        if os.name == "nt":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0   # (SW_HIDE)
            kw = {"startupinfo": si, "creationflags": subprocess.CREATE_NEW_CONSOLE}
        else:
            kw = {"start_new_session": True}
        env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        try:
            self.proc = subprocess.Popen([python_exe()] + self.args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, env=env, **kw)
        except OSError as e:
            self.failed_to_start = str(e)
            self.ended.emit(-1)
            return
        for raw in self.proc.stdout:
            self.line.emit(raw.decode("utf-8", "replace").rstrip("\r\n"))
        self.ended.emit(self.proc.wait())

    def stop(self):
        p = self.proc
        if not p or p.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            p.kill()


class Sender(QThread):
    """the title folder to /data/homebrew/<title id> on the console, over FTP"""
    progress = pyqtSignal(int, int)
    said = pyqtSignal(str)
    ended = pyqtSignal(str)   # ("" when it worked)

    def __init__(self, folder, host, port):
        super().__init__()
        self.folder, self.host, self.port = folder, host, port

    def run(self):
        try:
            title = os.path.basename(self.folder.rstrip("/\\"))
            files = []
            for base, _, names in os.walk(self.folder):
                for n in names:
                    p = os.path.join(base, n)
                    files.append((p, os.path.relpath(p, self.folder).replace(os.sep, "/")))
            # (eboot.bin and param.json last: the console lists the title once they are there)
            files.sort(key=lambda f: (f[1] in ("eboot.bin", "sce_sys/param.json"), f[1]))
            total = sum(os.path.getsize(p) for p, _ in files)
            sent = 0
            ftp = ftplib.FTP()
            ftp.connect(self.host, self.port, timeout=30)
            ftp.login()
            ftp.voidcmd("TYPE I")
            root = "/data/homebrew/" + title

            def mkdirs(path):
                at = ""
                for part in path.strip("/").split("/"):
                    at += "/" + part
                    try:
                        ftp.mkd(at)
                    except ftplib.all_errors:
                        pass
            mkdirs(root)
            for path, rel in files:
                if "/" in rel:
                    mkdirs(root + "/" + rel.rsplit("/", 1)[0])
                self.said.emit("sending " + rel)
                with open(path, "rb") as f:
                    def block(b):
                        nonlocal sent
                        sent += len(b)
                        self.progress.emit(sent >> 10, max(1, total >> 10))
                    ftp.storbinary("STOR " + root + "/" + rel, f, blocksize=1 << 20, callback=block)
            try:
                ftp.quit()
            except ftplib.all_errors:
                pass
            self.ended.emit("")
        except Exception as e:   # (shown to the player as it is)
            self.ended.emit("%s: %s" % (type(e).__name__, e))


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Longshot PS5 Builder")
        icon = os.path.join(HERE, "longshot.png")
        if os.path.exists(icon):
            self.setWindowIcon(QIcon(icon))
        self.settings = QSettings("PS5_Longshot", "Builder")
        self.proc = None
        self.sender = None
        self.built = ""
        self.stopping = False
        self.error = ""

        head = QLabel("Longshot PS5 Builder")
        f = QFont(self.font())
        f.setPointSize(f.pointSize() + 6)
        f.setBold(True)
        head.setFont(f)
        sub = QLabel("Builds the PlayStation 5 title folder from your own copy of the game. The build happens on this "
                     "computer; the folder it makes is yours alone and must not be shared.")
        sub.setWordWrap(True)

        self.game = QLineEdit(self.settings.value("game", ""))
        self.game.setPlaceholderText("Your game file (.nup)")
        game_pick = QPushButton("Choose…")
        game_pick.clicked.connect(self.pick_game)
        self.out = QLineEdit(self.settings.value("out", os.path.join(os.path.expanduser("~"), "Longshot-PS5")))
        out_pick = QPushButton("Choose…")
        out_pick.clicked.connect(self.pick_out)
        form = QFormLayout()
        form.addRow("Game file", self.row(self.game, game_pick))
        form.addRow("Build into", self.row(self.out, out_pick))
        # (the console's profile: pipeline.py MODELS. No model chosen until the player chooses one)
        self.model = QComboBox()
        self.model.addItem("Choose your console…", "")
        self.model.addItem("PS5 / PS5 Slim", "base")
        self.model.addItem("PS5 Pro", "pro")
        self.model.setCurrentIndex(max(0, self.model.findData(self.settings.value("model", ""))))
        self.model.setToolTip("The folder is built for one model. PS5 Pro: its faster processor mode and larger memory, "
                              "larger textures. PS5 / PS5 Slim: the base console's memory, smaller textures. A folder "
                              "built for the base console also runs on a Pro, in the base console's mode.")
        form.addRow("Console", self.model)
        self.jobs = QSpinBox()
        self.jobs.setRange(0, 64)
        self.jobs.setSpecialValueText("Automatic")
        self.jobs.setValue(int(self.settings.value("jobs", 0)))
        self.jobs.setToolTip("Files compiled at once, at the most. Automatic: one fewer than this computer's cores; how many "
                             "run is decided by the memory (Memory to use).")
        form.addRow("Compile at once", self.jobs)
        self.memory = QComboBox()
        self.memory.addItem("Automatic (what is free)", 0)
        for gb in (2, 3, 4, 6, 8, 12, 16):
            self.memory.addItem("%d GB" % gb, gb)
        self.memory.setCurrentIndex(max(0, self.memory.findData(int(self.settings.value("memory", 0)))))
        self.memory.setToolTip("The memory the build may take. Automatic: what is free, read again as it goes. A lower "
                               "figure builds on a computer with little memory, more slowly: 2 GB works, with other "
                               "programs closed. Choose lower if the build is stopped for want of memory.")
        form.addRow("Memory to use", self.memory)

        self.steps = []
        steps_box = QVBoxLayout()
        steps_box.setSpacing(2)
        for i, name in enumerate(STEPS, 1):
            l = QLabel()
            l.setTextFormat(Qt.TextFormat.RichText)
            self.steps.append(l)
            steps_box.addWidget(l)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.status = QLabel("Choose your game file, then Build.")
        self.status.setWordWrap(True)

        self.go = QPushButton("Build")
        self.go.setDefault(True)
        self.go.clicked.connect(self.start_or_stop)
        self.open = QPushButton("Open folder")
        self.open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.built)))
        self.open.setEnabled(False)
        self.details = QCheckBox("Show details")
        self.details.toggled.connect(lambda on: self.log.setVisible(on))
        buttons = QHBoxLayout()
        buttons.addWidget(self.details)
        buttons.addStretch(1)
        buttons.addWidget(self.open)
        buttons.addWidget(self.go)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.log.setFont(mono)
        self.log.setVisible(False)
        self.log.setMinimumHeight(180)

        # ---- the console ----
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        self.ps5 = QLineEdit(self.settings.value("ps5", ""))
        self.ps5.setPlaceholderText("Console's address, for example 192.168.1.50")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(self.settings.value("port", 2121)))
        self.send = QPushButton("Send to PS5")
        self.send.setEnabled(False)
        self.send.clicked.connect(self.start_send)
        send_row = QHBoxLayout()
        send_row.addWidget(self.ps5, 1)
        send_row.addWidget(QLabel("FTP port"))
        send_row.addWidget(self.port)
        send_row.addWidget(self.send)
        self.send_note = QLabel("When the build is done: copy the title folder to <b>/data/homebrew</b> on the console yourself, "
                                "or send it from here with the console's FTP server running. Your game file goes to "
                                "<b>/data/eglantineps5/roms</b> on the console.")
        self.send_note.setWordWrap(True)
        self.send_note.setTextFormat(Qt.TextFormat.RichText)

        box = QVBoxLayout(self)
        box.addWidget(head)
        box.addWidget(sub)
        box.addLayout(form)
        box.addLayout(steps_box)
        box.addWidget(self.bar)
        box.addWidget(self.status)
        box.addLayout(buttons)
        box.addWidget(self.log, 1)
        box.addWidget(line)
        box.addWidget(self.send_note)
        box.addLayout(send_row)
        self.resize(720, 520)
        self.show_steps(0)

    @staticmethod
    def row(edit, button):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        h.addWidget(button)
        return w

    def show_steps(self, current, failed=False, done=False):
        for i, l in enumerate(self.steps, 1):
            name = STEPS[i - 1]
            if done or i < current:
                l.setText("<span style='color:#2e9e5b'>✓</span> %s" % name)
            elif i == current:
                mark = "<span style='color:#c0392b'>✗</span>" if failed else "<b>▸</b>"
                l.setText("%s <b>%s</b> <span style='color:gray'>%s</span>" % (mark, name, "" if failed else ABOUT.get(i, "")))
            else:
                l.setText("<span style='color:gray'>○ %s</span>" % name)
        self.current = current

    def pick_game(self):
        start = os.path.dirname(self.game.text()) or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "Your game file", start, "Game files (*.nup);;All files (*)")
        if path:
            self.game.setText(path)

    def pick_out(self):
        path = QFileDialog.getExistingDirectory(self, "Build into", self.out.text() or os.path.expanduser("~"))
        if path:
            self.out.setText(path)

    def busy(self, on):
        for w in (self.game, self.out, self.jobs, self.memory, self.model):
            w.setEnabled(not on)
        self.go.setText("Stop" if on else "Build")

    def start_or_stop(self):
        if self.proc:
            if QMessageBox.question(self, "Stop the build?", "Stop the build? What it has done so far is kept: the next "
                                    "build goes on from there.") == QMessageBox.StandardButton.Yes and self.proc:
                self.stopping = True
                self.proc.stop()
            return
        game, out = self.game.text().strip(), self.out.text().strip()
        if not game or not os.path.exists(game):
            QMessageBox.warning(self, "No game file", "Choose your game file first: the .nup of your own copy of the game.")
            return
        if not out:
            QMessageBox.warning(self, "No folder", "Choose the folder to build into.")
            return
        model = self.model.currentData()
        if not model:
            QMessageBox.warning(self, "No console", "Choose your console: PS5 / PS5 Slim, or PS5 Pro.")
            return
        for k, v in (("game", game), ("out", out), ("jobs", self.jobs.value()), ("memory", self.memory.currentData()),
                     ("model", model)):
            self.settings.setValue(k, v)
        os.makedirs(out, exist_ok=True)
        self.log.clear()
        self.built = ""
        self.stopping = False
        self.open.setEnabled(False)
        self.send.setEnabled(False)
        self.bar.setRange(0, 0)
        self.status.setText("Building…")
        self.show_steps(1)
        self.busy(True)
        args = ["-I", "-u", PIPELINE, "--game", game, "--out", out, "--model", model]
        if self.jobs.value():
            args += ["--jobs", str(self.jobs.value())]
        if self.memory.currentData():
            args += ["--memory", str(self.memory.currentData())]
        self.error = ""
        self.proc = Build(args)
        self.proc.line.connect(self.line)
        self.proc.ended.connect(self.finished)
        self.proc.start()

    def line(self, line):
        if line.startswith("@@step "):
            n = int(line.split()[1])
            self.show_steps(n)
            self.bar.setRange(0, 0)
            self.status.setText(STEPS[n - 1] + "…")
        elif line.startswith("@@progress "):
            _, done, total = line.split()
            self.bar.setRange(0, int(total))
            self.bar.setValue(int(done))
            self.status.setText("%s: %s of %s files" % (STEPS[self.current - 1], done, total))
        elif line.startswith("@@done "):
            self.built = line[7:]
        elif line.startswith("@@error "):
            self.error = line[8:].replace(" | ", "\n")
        else:
            self.log.appendPlainText(line)

    def fail(self, text):
        self.show_steps(max(1, self.current), failed=True)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.status.setText(text.split("\n")[0])
        self.details.setChecked(True)
        self.log.appendPlainText("\n" + text)
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    def finished(self, code):
        if self.proc and self.proc.failed_to_start:
            self.error = "The builder's own Python did not start (%s): %s" % (python_exe(), self.proc.failed_to_start)
        if self.proc:
            self.proc.wait()
        self.proc = None
        self.busy(False)
        if self.built and code == 0:
            self.show_steps(len(STEPS) + 1, done=True)
            self.bar.setRange(0, 1)
            self.bar.setValue(1)
            self.status.setText("Done. Your title folder: %s" % self.built)
            self.open.setEnabled(True)
            self.send.setEnabled(True)
        elif self.stopping:
            self.show_steps(self.current, failed=True)
            self.bar.setRange(0, 1)
            self.bar.setValue(0)
            self.status.setText("Stopped. Build again to go on from where it was.")
        else:
            self.fail(getattr(self, "error", "") or "The build stopped (exit %d). The details say where." % code)
        self.error = ""

    def start_send(self):
        host = self.ps5.text().strip()
        if not host:
            QMessageBox.warning(self, "No address", "Type the console's address: its IP on your network.")
            return
        self.settings.setValue("ps5", host)
        self.settings.setValue("port", self.port.value())
        self.send.setEnabled(False)
        self.go.setEnabled(False)
        self.status.setText("Sending to the console…")
        self.sender = Sender(self.built, host, self.port.value())
        self.sender.progress.connect(lambda a, b: (self.bar.setRange(0, b), self.bar.setValue(a)))
        self.sender.said.connect(self.log.appendPlainText)
        self.sender.ended.connect(self.sent)
        self.sender.start()

    def sent(self, error):
        self.send.setEnabled(True)
        self.go.setEnabled(True)
        if error:
            self.status.setText("Not sent: %s" % error)
            QMessageBox.warning(self, "Not sent", "The folder was not sent to the console.\n\n%s\n\nIs the console on, with its "
                                "FTP server running, at this address and port?" % error)
        else:
            self.status.setText("Sent: %s is in /data/homebrew on the console. Start Longshot from its home screen." %
                                os.path.basename(self.built))

    def closeEvent(self, e):
        if self.proc:
            if QMessageBox.question(self, "Stop the build?", "A build is running. Stop it and close?") != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.proc.stop()
            self.proc.wait(5000)
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Longshot PS5 Builder")
    w = Window()
    w.show()
    if "--self-test" in sys.argv:
        # (the release workflow's: the window made and shown, the build's own check run through it as a build is, then closed.
        # Exit 0 when the check ended well)
        from PyQt6.QtCore import QTimer
        result = {"code": 1}
        log = []
        check = Build(["-I", "-u", PIPELINE, "--check"])
        check.line.connect(lambda line: (log.append(line), print(line, flush=True)))
        check.ended.connect(lambda code: (result.update(code=0 if code == 0 and any(l == "@@done check" for l in log) else 1), app.quit()))
        QTimer.singleShot(200, check.start)
        QTimer.singleShot(240000, app.quit)
        app.exec()
        check.wait(5000)
        print("self-test: the window opened (%dx%d), the check %s" % (w.width(), w.height(), "passed" if result["code"] == 0 else "FAILED"), flush=True)
        return result["code"]
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
