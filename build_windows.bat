@echo off
REM Run on Windows with Python 3.10+ installed. Produces output\GeminiAssistantSetup.exe
pip install -r requirements.txt || exit /b 1
pyinstaller --noconfirm --windowed --name GeminiAssistant --collect-all vosk --collect-all sounddevice --hidden-import pyttsx3.drivers.sapi5 --hidden-import keyring.backends.Windows gemini_assistant.py || exit /b 1
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
echo Done. Installer: output\GeminiAssistantSetup.exe
