; Inno Setup Script for Whisper Dictation
; Download Inno Setup: https://jrsoftware.org/isinfo.php
;
; To build the installer:
;   1. First run: python build.py
;   2. Then compile this .iss file with Inno Setup Compiler
;
; Output: Output/WhisperDictation_Setup.exe

[Setup]
AppName=Whisper Dictation
AppVersion=1.0.0
AppPublisher=Robin
DefaultDirName={autopf}\WhisperDictation
DefaultGroupName=Whisper Dictation
OutputBaseFilename=WhisperDictation_Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
; Uncomment if you have an icon:
; SetupIconFile=assets\icon.ico

[Files]
; Bundle everything from the PyInstaller dist folder
Source: "dist\WhisperDictation\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
; Start Menu shortcut
Name: "{group}\Whisper Dictation"; Filename: "{app}\WhisperDictation.exe"
Name: "{group}\Uninstall Whisper Dictation"; Filename: "{uninstallexe}"
; Desktop shortcut (optional)
Name: "{commondesktop}\Whisper Dictation"; Filename: "{app}\WhisperDictation.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start automatically with Windows"; GroupDescription: "Options:"

[Registry]
; Auto-start with Windows (only if user selected the task)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "WhisperDictation"; \
  ValueData: """{app}\WhisperDictation.exe"""; \
  Flags: uninsdeletevalue; Tasks: startup

[Run]
; Offer to launch after install
Filename: "{app}\WhisperDictation.exe"; Description: "Launch Whisper Dictation"; \
  Flags: nowait postinstall skipifsilent
