"""
Gemini Desktop Assistant - background app (tray icon, hotkeys, "Hey Gemini" wake word).
Install with the Windows installer (see README) - no Python, npm or scripts needed by the user.
"""
import array
import io
import json
import os
import queue
import shlex
import sys
import tempfile
import threading
import time
import urllib.request
import wave
import zipfile
from pathlib import Path

import keyboard
import mss
import pyautogui
import pyperclip
import sounddevice as sd
from google import genai
from google.genai import types
from PIL import Image
from PyQt6.QtCore import QLockFile, QObject, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QAction, QColor, QCursor, QFont, QGuiApplication, QIcon,
                         QPainter, QPixmap)
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFormLayout,
                             QHBoxLayout, QLineEdit, QMenu, QMessageBox, QPushButton,
                             QSystemTrayIcon, QTextBrowser, QVBoxLayout, QWidget)

try:
    import pyttsx3
except Exception:
    pyttsx3 = None

APP = "GeminiAssistant"
MODELS = ["gemini-2.5-flash", "gemini-2.5-pro"]
WAKE_PHRASE = "hey gemini"
VOSK_NAME = "vosk-model-small-en-us-0.15"
VOSK_URL = f"https://alphacephei.com/vosk/models/{VOSK_NAME}.zip"
PASTE_KEY = "command" if sys.platform == "darwin" else "ctrl"
pyautogui.FAILSAFE = True

# ------------------------------------------------------------------ config
CFG_DIR = (Path(os.environ["APPDATA"]) / APP) if sys.platform == "win32" else Path.home() / ".config" / APP
CFG_DIR.mkdir(parents=True, exist_ok=True)
CFG_FILE = CFG_DIR / "config.json"
cfg = {"model": MODELS[0], "hotkey": "ctrl+space", "voice_hotkey": "ctrl+alt+space",
       "wake_word": True, "autostart": True, "api_key": ""}
try:
    cfg.update(json.loads(CFG_FILE.read_text()))
except Exception:
    pass


def save_cfg():
    CFG_FILE.write_text(json.dumps(cfg, indent=2))


def get_key() -> str:
    try:
        import keyring
        k = keyring.get_password(APP, "api_key")
        if k:
            return k
    except Exception:
        pass
    return cfg.get("api_key", "")


def set_key(k: str):
    try:
        import keyring
        keyring.set_password(APP, "api_key", k)
        cfg["api_key"] = ""
    except Exception:
        cfg["api_key"] = k
    save_cfg()


client = None


def make_client():
    global client
    k = get_key()
    client = genai.Client(api_key=k) if k else None


def set_autostart(on: bool):
    cmd = (f'"{sys.executable}"' if getattr(sys, "frozen", False)
           else f'"{sys.executable}" "{os.path.abspath(__file__)}"')
    if sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, APP)
                except FileNotFoundError:
                    pass
    elif sys.platform == "darwin":
        p = Path.home() / "Library/LaunchAgents/com.gemini.assistant.plist"
        if on:
            args = "".join(f"<string>{a}</string>" for a in shlex.split(cmd))
            p.write_text('<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
                         '<key>Label</key><string>com.gemini.assistant</string>'
                         f'<key>ProgramArguments</key><array>{args}</array>'
                         '<key>RunAtLoad</key><true/></dict></plist>')
        elif p.exists():
            p.unlink()
    else:
        p = Path.home() / ".config/autostart/gemini-assistant.desktop"
        if on:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"[Desktop Entry]\nType=Application\nName=Gemini Assistant\nExec={cmd}\n")
        elif p.exists():
            p.unlink()


# ------------------------------------------------------------------ gemini prompt
def system_prompt(agent: bool) -> str:
    base = (
        "You are a desktop assistant that can see the user's screenshot. The user may be in ANY "
        "app (Word, Excel, browser, Photoshop, code editor, settings, games...). Answer concisely "
        "with exact, visible menu/tab/button names and shortcuts. Reply ONLY with JSON: "
        "{\"answer\": markdown string, \"highlights\": [{\"label\": str, \"box_2d\": "
        "[ymin,xmin,ymax,xmax]}], \"actions\": []}. box_2d values are normalized 0-1000 relative to "
        "the screenshot. Put in highlights every UI element the user should look at or click."
    )
    if agent:
        base += (
            " In 'actions' you may list steps to perform for the user, each one of: "
            "{\"type\":\"click|double_click|right_click\",\"target\":[ymin,xmin,ymax,xmax],\"why\":str}, "
            "{\"type\":\"type\",\"text\":str}, {\"type\":\"hotkey\",\"keys\":[\"ctrl\",\"s\"]}, "
            "{\"type\":\"scroll\",\"amount\":int}, {\"type\":\"wait\",\"seconds\":number}. "
            "Only use actions when the user clearly wants you to DO it; max 8 steps; never do "
            "anything destructive or involving passwords/payments."
        )
    else:
        base += " Always return \"actions\": []."
    return base


# ------------------------------------------------------------------ voice engine (one mic stream)
class VoiceEngine(QObject):
    audio_ready = pyqtSignal(bytes)
    wake = pyqtSignal()
    cancelled = pyqtSignal()
    status = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.q = queue.Queue()
        self.recording = False
        self.force_stop = False
        self.wake_enabled = False
        self.frames, self.heard, self.quiet, self.t0 = [], False, 0, 0.0

    def begin(self):
        threading.Thread(target=self._run, daemon=True).start()

    def start_recording(self):
        if self.recording:
            return
        self.frames, self.heard, self.quiet, self.t0 = [], False, 0, time.time()
        self.force_stop = False
        self.recording = True

    def stop_recording(self):
        self.force_stop = True

    def _load_recognizer(self):
        try:
            import vosk
        except Exception:
            self.status.emit("Wake word unavailable (vosk missing).")
            return None
        path = CFG_DIR / VOSK_NAME
        if not path.exists():
            self.status.emit("Downloading wake-word model (one time, ~40 MB)...")
            z = CFG_DIR / "model.zip"
            urllib.request.urlretrieve(VOSK_URL, z)
            with zipfile.ZipFile(z) as f:
                f.extractall(CFG_DIR)
            z.unlink()
        vosk.SetLogLevel(-1)
        return vosk.KaldiRecognizer(vosk.Model(str(path)), 16000, json.dumps([WAKE_PHRASE, "[unk]"]))

    def _run(self):
        rec, failed = None, False
        try:
            with sd.RawInputStream(samplerate=16000, blocksize=4000, channels=1, dtype="int16",
                                   callback=lambda d, *_: self.q.put(bytes(d))):
                while True:
                    chunk = self.q.get()
                    if self.recording:
                        self._record(chunk)
                        continue
                    if self.wake_enabled and rec is None and not failed:
                        try:
                            rec = self._load_recognizer()
                        except Exception as e:  # noqa: BLE001
                            self.status.emit(f"Wake word setup failed: {e}")
                        failed = rec is None
                    if self.wake_enabled and rec:
                        if rec.AcceptWaveform(chunk):
                            text = json.loads(rec.Result()).get("text", "")
                        else:
                            text = json.loads(rec.PartialResult()).get("partial", "")
                        if WAKE_PHRASE in text:
                            rec.Reset()
                            self.start_recording()
                            self.wake.emit()
        except Exception as e:  # noqa: BLE001
            self.status.emit(f"Microphone error: {e}")

    def _record(self, chunk: bytes):
        self.frames.append(chunk)
        a = array.array("h", chunk)
        rms = (sum(x * x for x in a) / max(len(a), 1)) ** 0.5
        if rms > 600:
            self.heard, self.quiet = True, 0
        elif self.heard:
            self.quiet += 1
        elapsed = time.time() - self.t0
        if not self.heard and elapsed > 7 and not self.force_stop:
            self.recording = False
            self.cancelled.emit()
            return
        if self.force_stop or (self.heard and self.quiet >= 5) or elapsed > 20:
            self.recording = False
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes(b"".join(self.frames))
            self.audio_ready.emit(buf.getvalue())


# ------------------------------------------------------------------ screen helpers
class Overlay(QWidget):
    def __init__(self, screen, marks):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setGeometry(screen.geometry())
        self.marks = marks
        QTimer.singleShot(9000, self.close)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        color = QColor("#ff3b6b")
        for i, m in enumerate(self.marks, 1):
            try:
                y1, x1, y2, x2 = [float(v) for v in m["box_2d"]]
            except Exception:
                continue
            r = QRectF(x1 / 1000 * w, y1 / 1000 * h, (x2 - x1) / 1000 * w, (y2 - y1) / 1000 * h)
            p.setPen(color); p.setBrush(QColor(255, 59, 107, 40))
            pen = p.pen(); pen.setWidth(4); p.setPen(pen)
            p.drawRoundedRect(r, 8, 8)
            label = f"{i}. {m.get('label', '')}"
            tag = QRectF(r.x(), max(0, r.y() - 28), 8.5 * len(label) + 18, 26)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(color)
            p.drawRoundedRect(tag, 6, 6)
            p.setPen(QColor("white")); p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
            p.drawText(tag, Qt.AlignmentFlag.AlignCenter, label)


def current_monitor():
    x, y = pyautogui.position()
    with mss.mss() as sct:
        for m in sct.monitors[1:]:
            if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
                return dict(m)
        return dict(sct.monitors[1])


def grab(mon) -> bytes:
    with mss.mss() as sct:
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    img.thumbnail((1920, 1920))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def describe(a: dict) -> str:
    t = a.get("type")
    if t in ("click", "double_click", "right_click"):
        return f"{t.replace('_', ' ')} - {a.get('why', 'on-screen element')}"
    if t == "type":
        return f"type: {a.get('text', '')[:60]}"
    if t == "hotkey":
        return "press " + "+".join(a.get("keys", []))
    return f"{t} {a.get('amount', a.get('seconds', ''))}"


def run_actions(actions, mon):
    for a in actions:
        t = a.get("type")
        if t in ("click", "double_click", "right_click"):
            y1, x1, y2, x2 = a["target"]
            x = mon["left"] + (x1 + x2) / 2000 * mon["width"]
            y = mon["top"] + (y1 + y2) / 2000 * mon["height"]
            pyautogui.moveTo(x, y, duration=0.35)
            {"click": pyautogui.click, "double_click": pyautogui.doubleClick,
             "right_click": pyautogui.rightClick}[t]()
        elif t == "type":
            pyperclip.copy(a.get("text", ""))
            pyautogui.hotkey(PASTE_KEY, "v")
        elif t == "hotkey":
            pyautogui.hotkey(*a.get("keys", []))
        elif t == "scroll":
            pyautogui.scroll(int(a.get("amount", 0)))
        elif t == "wait":
            time.sleep(min(float(a.get("seconds", 1)), 5))
        time.sleep(0.5)


# ------------------------------------------------------------------ UI
class Bridge(QObject):
    hotkey = pyqtSignal(bool)
    result = pyqtSignal(dict)
    error = pyqtSignal(str)
    finished_actions = pyqtSignal()


class Settings(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Gemini Assistant - Settings")
        self.setMinimumWidth(460)
        self.key = QLineEdit(get_key()); self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Paste key from aistudio.google.com/apikey")
        self.hk = QLineEdit(cfg["hotkey"])
        self.vhk = QLineEdit(cfg["voice_hotkey"])
        self.wake = QCheckBox('Wake word: say "Hey Gemini"'); self.wake.setChecked(cfg["wake_word"])
        self.auto = QCheckBox("Start automatically when I log in"); self.auto.setChecked(cfg["autostart"])
        ok = QPushButton("Save"); ok.clicked.connect(self.accept)
        f = QFormLayout(self)
        f.addRow("Gemini API key", self.key)
        f.addRow("Open hotkey", self.hk)
        f.addRow("Voice hotkey", self.vhk)
        f.addRow(self.wake); f.addRow(self.auto); f.addRow(ok)


class Assistant(QWidget):
    def __init__(self, bridge: Bridge, engine: VoiceEngine):
        super().__init__()
        self.bridge, self.engine = bridge, engine
        self.history, self.overlay, self.drag = [], None, None
        self.mon, self.qscreen, self.pending_q = None, None, ""
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.resize(620, 440)
        self.setStyleSheet("""
            QWidget{background:#16181d;color:#e8eaf0;font-family:'Segoe UI';font-size:13px}
            QLineEdit{background:#22252d;border:1px solid #3a3f4b;border-radius:10px;padding:10px;font-size:15px}
            QTextBrowser{background:#1b1e25;border:none;border-radius:10px;padding:8px}
            QPushButton{background:#2b3040;border:none;border-radius:8px;padding:8px 12px}
            QPushButton:hover{background:#3a4156}
            QComboBox{background:#22252d;border-radius:6px;padding:4px}
        """)
        self.input = QLineEdit(placeholderText="Ask about anything on your screen...  (Enter = send, Esc = hide)")
        self.mic, self.send = QPushButton("🎤"), QPushButton("Ask")
        self.out = QTextBrowser(); self.out.setOpenExternalLinks(True)
        self.agent = QCheckBox("Agent mode (can click/type, asks first)")
        self.speak = QCheckBox("Speak answers")
        self.model = QComboBox(); self.model.addItems(MODELS); self.model.setCurrentText(cfg["model"])
        new = QPushButton("New chat")
        top = QHBoxLayout()
        for w in (self.input, self.mic, self.send):
            top.addWidget(w)
        bottom = QHBoxLayout()
        for w in (self.agent, self.speak, self.model, new):
            bottom.addWidget(w)
        lay = QVBoxLayout(self); lay.addLayout(top); lay.addWidget(self.out); lay.addLayout(bottom)

        self.input.returnPressed.connect(lambda: self.ask(self.input.text()))
        self.send.clicked.connect(lambda: self.ask(self.input.text()))
        self.mic.clicked.connect(self.toggle_voice)
        new.clicked.connect(self.new_chat)
        self.model.currentTextChanged.connect(lambda m: (cfg.update(model=m), save_cfg()))
        bridge.hotkey.connect(self.pop)
        bridge.result.connect(self.show_result)
        bridge.error.connect(lambda e: self.out.setMarkdown(f"**Error:** {e}"))
        bridge.finished_actions.connect(self.show)
        engine.wake.connect(lambda: self.pop(False, from_wake=True))
        engine.audio_ready.connect(lambda b: self.ask("", audio=b))
        engine.cancelled.connect(lambda: self.out.setMarkdown("Didn't hear anything. Try again."))
        engine.status.connect(lambda s: self.out.setMarkdown(s))
        self.out.setMarkdown('Ready. Ask about your screen, or say **"Hey Gemini"**.')

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.engine.force_stop = self.engine.recording
            self.hide()

    def mousePressEvent(self, e):
        self.drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self.drag is not None:
            self.move(e.globalPosition().toPoint() - self.drag)

    def pop(self, voice: bool, from_wake: bool = False):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        g = screen.availableGeometry()
        self.move(g.center().x() - self.width() // 2, g.top() + 80)
        self.show(); self.raise_(); self.activateWindow(); self.input.setFocus()
        if from_wake or voice:
            if voice:
                self.engine.start_recording()
            QApplication.beep()
            self.out.setMarkdown("🎙 **Listening...** just ask your question.")
            self.mic.setText("⏹")

    def new_chat(self):
        self.history = []
        self.out.setMarkdown("New conversation started.")

    def toggle_voice(self):
        if self.engine.recording:
            self.engine.stop_recording()
        else:
            self.engine.start_recording()
            self.mic.setText("⏹")
            self.out.setMarkdown("🎙 **Listening...**")

    def ask(self, question: str, audio: bytes | None = None):
        question = question.strip()
        self.mic.setText("🎤")
        if not question and not audio:
            return
        if client is None:
            self.out.setMarkdown("Add your Gemini API key in **Settings** (tray icon → Settings).")
            return
        self.input.clear()
        self.mon = current_monitor()
        self.qscreen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        self.hide(); QApplication.processEvents(); time.sleep(0.25)
        png = grab(self.mon)
        self.show()
        self.out.setMarkdown("⏳ Thinking...")
        self.pending_q = question or "(voice question)"
        args = (question, audio, png, self.agent.isChecked(), self.model.currentText(), list(self.history))
        threading.Thread(target=self.worker, args=args, daemon=True).start()

    def worker(self, question, audio, png, agent, model, history):
        try:
            contents = [types.Content(role=r, parts=[types.Part.from_text(text=t)]) for r, t in history]
            parts = [types.Part.from_bytes(data=png, mime_type="image/png")]
            if audio:
                parts += [types.Part.from_bytes(data=audio, mime_type="audio/wav"),
                          types.Part.from_text(text="The user's question is spoken in this audio.")]
            else:
                parts.append(types.Part.from_text(text=question))
            contents.append(types.Content(role="user", parts=parts))
            resp = client.models.generate_content(
                model=model, contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt(agent), response_mime_type="application/json"))
            try:
                data = json.loads(resp.text)
            except Exception:
                data = {"answer": resp.text, "highlights": [], "actions": []}
            data["agent"] = agent
            self.bridge.result.emit(data)
        except Exception as e:  # noqa: BLE001
            self.bridge.error.emit(str(e))

    def show_result(self, data: dict):
        answer = data.get("answer", "")
        self.out.setMarkdown(answer)
        self.history = (self.history + [("user", self.pending_q), ("model", answer)])[-12:]
        marks = [m for m in data.get("highlights", []) if isinstance(m, dict)]
        if marks:
            self.overlay = Overlay(self.qscreen, marks)
            self.overlay.show()
        if self.speak.isChecked() and pyttsx3:
            def say():
                e = pyttsx3.init(); e.say(answer[:500]); e.runAndWait()
            threading.Thread(target=say, daemon=True).start()
        actions = data.get("actions") or []
        if actions and data.get("agent"):
            plan = "\n".join(f"{i}. {describe(a)}" for i, a in enumerate(actions, 1))
            ok = QMessageBox.question(self, "Run these steps?",
                                      plan + "\n\n(Move the mouse into a screen corner to abort.)")
            if ok == QMessageBox.StandardButton.Yes:
                self.hide()

                def go():
                    time.sleep(0.4)
                    try:
                        run_actions(actions, self.mon)
                    except Exception as e:  # noqa: BLE001
                        self.bridge.error.emit(f"Action stopped: {e}")
                    self.bridge.finished_actions.emit()
                threading.Thread(target=go, daemon=True).start()


def make_icon() -> QIcon:
    pm = QPixmap(64, 64); pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#4f7cff")); p.setPen(Qt.PenStyle.NoPen); p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("white")); p.setFont(QFont("Segoe UI", 28, QFont.Weight.Bold))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "G"); p.end()
    return QIcon(pm)


# ------------------------------------------------------------------ main
def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    lock = QLockFile(os.path.join(tempfile.gettempdir(), "gemini_assistant.lock"))
    if not lock.tryLock(100):
        sys.exit(0)  # already running

    make_client()
    bridge, engine = Bridge(), VoiceEngine()
    win = Assistant(bridge, engine)
    engine.wake_enabled = cfg["wake_word"]
    engine.begin()

    handles = []

    def register_hotkeys():
        for h in handles:
            try:
                keyboard.remove_hotkey(h)
            except Exception:
                pass
        handles.clear()
        try:
            handles.append(keyboard.add_hotkey(cfg["hotkey"], lambda: bridge.hotkey.emit(False)))
            handles.append(keyboard.add_hotkey(cfg["voice_hotkey"], lambda: bridge.hotkey.emit(True)))
        except Exception as e:  # noqa: BLE001
            tray.showMessage("Hotkey problem", str(e))

    def open_settings():
        d = Settings(win)
        if d.exec():
            set_key(d.key.text().strip())
            cfg.update(hotkey=d.hk.text().strip() or "ctrl+space",
                       voice_hotkey=d.vhk.text().strip() or "ctrl+alt+space",
                       wake_word=d.wake.isChecked(), autostart=d.auto.isChecked())
            save_cfg()
            make_client()
            engine.wake_enabled = cfg["wake_word"]
            register_hotkeys()
            try:
                set_autostart(cfg["autostart"])
            except Exception as e:  # noqa: BLE001
                tray.showMessage("Autostart problem", str(e))

    tray = QSystemTrayIcon(make_icon(), app)
    menu = QMenu()
    for text, fn in (("Open assistant", lambda: win.pop(False)), ("Settings...", open_settings),
                     ("Quit", app.quit)):
        act = QAction(text, menu); act.triggered.connect(fn); menu.addAction(act)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: win.pop(False) if r == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.setToolTip("Gemini Assistant")
    tray.show()

    register_hotkeys()
    if not get_key():
        open_settings()
    else:
        tray.showMessage("Gemini Assistant is running",
                         f'Press {cfg["hotkey"]} or say "Hey Gemini".')
    if cfg["autostart"]:
        try:
            set_autostart(True)
        except Exception:
            pass
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
