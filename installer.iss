[Setup]
AppName=Gemini Desktop Assistant
AppVersion=1.0
DefaultDirName={autopf}\GeminiAssistant
DefaultGroupName=Gemini Desktop Assistant
OutputDir=output
OutputBaseFilename=GeminiAssistantSetup
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest

[Files]
Source: "dist\GeminiAssistant\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Gemini Assistant"; Filename: "{app}\GeminiAssistant.exe"
Name: "{autodesktop}\Gemini Assistant"; Filename: "{app}\GeminiAssistant.exe"

[Run]
Filename: "{app}\GeminiAssistant.exe"; Description: "Start Gemini Assistant now"; Flags: nowait postinstall skipifsilent
