; ============================================================================
;  Open Desk Bot V2 - standard Windows installer (NSIS / MUI2)
; ----------------------------------------------------------------------------
;  Wraps the already-built portable client EXE (service\dist\OpenDeskBotV2.exe,
;  the "green" self-extracting tray app) into a conventional Windows installer
;  that lands the program under $PROGRAMFILES64, registers a proper Add/Remove
;  Programs uninstall entry, creates shortcuts, and (running elevated) does the
;  two host-hardening steps we used to ask the user to run by hand: a Defender
;  folder exclusion and an inbound firewall allow rule - both best-effort and
;  silent, both reversed on uninstall.
;
;  Variables are supplied by Build-Client.ps1 through makensis /D defines; the
;  !ifndef fallbacks below let installer.nsi also be compiled by hand from this
;  directory (paths are relative to service\client\).
; ============================================================================

Unicode true

; ---- Build-time inputs (overridable via makensis /D...) ---------------------
!ifndef ODB_VERSION
  !define ODB_VERSION "2.0.0.0"
!endif
!ifndef ODB_SOURCE_EXE
  !define ODB_SOURCE_EXE "..\dist\OpenDeskBotV2.exe"
!endif
!ifndef ODB_ICON
  !define ODB_ICON "app.ico"
!endif
!ifndef ODB_OUTFILE
  !define ODB_OUTFILE "..\dist\OpenDeskBotV2-Setup.exe"
!endif
!ifndef ODB_LICENSE
  !define ODB_LICENSE "..\LICENSE"
!endif

; ---- Fixed product identity -------------------------------------------------
!define PRODUCT_NAME      "Open Desk Bot V2"
!define PRODUCT_PUBLISHER "OpenDeskBot Contributors"
!define PRODUCT_EXE       "OpenDeskBotV2.exe"
!define PRODUCT_DIRNAME   "OpenDeskBotV2"
!define PRODUCT_DATADIR   "$LOCALAPPDATA\OpenDeskBotV2"
!define UNINSTKEY         "Software\Microsoft\Windows\CurrentVersion\Uninstall\OpenDeskBotV2"
!define RUNKEY            "Software\Microsoft\Windows\CurrentVersion\Run"
!define RUNVALUE          "OpenDeskBotV2"
!define SETUP_MUTEX       "OpenDeskBotV2-Setup-Mutex"
!define FW_RULE_NAME      "Open Desk Bot V2"

; ---- Compression ------------------------------------------------------------
; The wrapped EXE is itself a compressed zip payload: /SOLID lzma reclaimed
; only ~3% while making both the packaging step and the install-time
; extraction painfully slow (minutes for ~363 MB). Store files uncompressed:
; extraction becomes a plain disk copy and installs in seconds.
SetCompress off

Name "${PRODUCT_NAME}"
OutFile "${ODB_OUTFILE}"
InstallDir "$PROGRAMFILES64\${PRODUCT_DIRNAME}"
InstallDirRegKey HKLM "${UNINSTKEY}" "InstallLocation"
RequestExecutionLevel admin
ShowInstDetails show
ShowUninstDetails show

; ---- Version resource stamped into the setup EXE ----------------------------
VIProductVersion "${ODB_VERSION}"
VIAddVersionKey "ProductName"     "${PRODUCT_NAME}"
VIAddVersionKey "ProductVersion"  "${ODB_VERSION}"
VIAddVersionKey "FileVersion"     "${ODB_VERSION}"
VIAddVersionKey "CompanyName"     "${PRODUCT_PUBLISHER}"
VIAddVersionKey "LegalCopyright"  "Copyright (C) 2026 ${PRODUCT_PUBLISHER}"
VIAddVersionKey "FileDescription" "${PRODUCT_NAME} Setup"

; ---- MUI2 -------------------------------------------------------------------
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

!define MUI_ICON   "${ODB_ICON}"
!define MUI_UNICON "${ODB_ICON}"
!define MUI_ABORTWARNING
!define MUI_LANGDLL_ALLLANGUAGES

; Installer pages
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${ODB_LICENSE}"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${PRODUCT_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "$(RUN_NOW)"
!insertmacro MUI_PAGE_FINISH

; Uninstaller pages
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; Languages (the selection dialog is shown from .onInit)
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_RESERVEFILE_LANGDLL

; ---- Localized strings ------------------------------------------------------
LangString SEC_CORE       ${LANG_SIMPCHINESE} "Open Desk Bot V2（必需）"
LangString SEC_CORE       ${LANG_ENGLISH}     "Open Desk Bot V2 (required)"
LangString SEC_DESKTOP    ${LANG_SIMPCHINESE} "创建桌面快捷方式"
LangString SEC_DESKTOP    ${LANG_ENGLISH}     "Create a desktop shortcut"
LangString SEC_STARTUP    ${LANG_SIMPCHINESE} "开机自启动"
LangString SEC_STARTUP    ${LANG_ENGLISH}     "Start automatically at logon"

LangString DESC_CORE      ${LANG_SIMPCHINESE} "安装 Open Desk Bot V2 主程序、开始菜单快捷方式与卸载信息。"
LangString DESC_CORE      ${LANG_ENGLISH}     "Install the Open Desk Bot V2 program, Start Menu shortcut and uninstaller."
LangString DESC_DESKTOP   ${LANG_SIMPCHINESE} "在桌面创建 Open Desk Bot V2 的快捷方式。"
LangString DESC_DESKTOP   ${LANG_ENGLISH}     "Place an Open Desk Bot V2 shortcut on the desktop."
LangString DESC_STARTUP   ${LANG_SIMPCHINESE} "登录 Windows 后自动启动 Open Desk Bot V2。"
LangString DESC_STARTUP   ${LANG_ENGLISH}     "Launch Open Desk Bot V2 automatically after you sign in to Windows."

LangString RUN_NOW        ${LANG_SIMPCHINESE} "立即运行 Open Desk Bot V2"
LangString RUN_NOW        ${LANG_ENGLISH}     "Run Open Desk Bot V2 now"

LangString ALREADY_RUNNING ${LANG_SIMPCHINESE} "安装程序已在运行。"
LangString ALREADY_RUNNING ${LANG_ENGLISH}     "The installer is already running."

LangString PREPARING_RUNTIME ${LANG_SIMPCHINESE} "正在展开运行环境（首次启动将直接可用）…"
LangString PREPARING_RUNTIME ${LANG_ENGLISH}     "Preparing the runtime (first launch will start instantly)..."

LangString DATA_PROMPT    ${LANG_SIMPCHINESE} "是否同时删除用户数据（配置、数据库、日志）？$\n$\n位置：${PRODUCT_DATADIR}$\n$\n选择“否”将保留数据，便于以后重新安装。"
LangString DATA_PROMPT    ${LANG_ENGLISH}     "Also delete user data (settings, databases, logs)?$\n$\nLocation: ${PRODUCT_DATADIR}$\n$\nChoose No to keep your data for a future reinstall."

; ============================================================================
;  Install sections
; ============================================================================
Section "$(SEC_CORE)" SecCore
  SectionIn RO

  ; Stop any running client so an in-place upgrade can overwrite the EXE.
  ; Best-effort: the app may not be running. /T also drops its child services.
  nsExec::Exec 'taskkill /F /T /IM "${PRODUCT_EXE}"'
  Pop $0
  Sleep 500

  SetOutPath "$INSTDIR"
  ; The portable client EXE is the installer's single released File (~363 MB).
  File "/oname=${PRODUCT_EXE}" "${ODB_SOURCE_EXE}"

  ; Run the whole first-run pipeline NOW (payload extraction to the user's
  ; %LOCALAPPDATA%, profile seeding, stable-bin materialization and — since
  ; this process is already elevated — the two path-based firewall rules), so
  ; the user's first real launch is instant with no extraction wait and no UAC.
  ; Best-effort: a non-zero exit just means first launch falls back to the
  ; normal in-launcher extraction.
  DetailPrint "$(PREPARING_RUNTIME)"
  ExecWait '"$INSTDIR\${PRODUCT_EXE}" --prepare-runtime' $0
  DetailPrint "prepare-runtime exit=$0"

  ; Start Menu shortcut is always created; icon comes from the EXE's own
  ; embedded app.ico (index 0), so no separate .ico file is shipped.
  CreateShortcut "$SMPROGRAMS\${PRODUCT_NAME}.lnk" "$INSTDIR\${PRODUCT_EXE}" "" "$INSTDIR\${PRODUCT_EXE}" 0

  ; Uninstaller + Add/Remove Programs registration (HKLM, machine-wide).
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr   HKLM "${UNINSTKEY}" "DisplayName"     "${PRODUCT_NAME}"
  WriteRegStr   HKLM "${UNINSTKEY}" "DisplayVersion"  "${ODB_VERSION}"
  WriteRegStr   HKLM "${UNINSTKEY}" "DisplayIcon"     "$INSTDIR\${PRODUCT_EXE},0"
  WriteRegStr   HKLM "${UNINSTKEY}" "Publisher"       "${PRODUCT_PUBLISHER}"
  WriteRegStr   HKLM "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKLM "${UNINSTKEY}" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
  WriteRegStr   HKLM "${UNINSTKEY}" "QuietUninstallString" "$\"$INSTDIR\Uninstall.exe$\" /S"
  WriteRegDWORD HKLM "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINSTKEY}" "NoRepair" 1

  ; EstimatedSize (KB) for Add/Remove Programs.
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKLM "${UNINSTKEY}" "EstimatedSize" $0

  ; --- Host hardening (elevated, best-effort, silent) ----------------------
  ; (a) Exclude the runtime/data folder from Microsoft Defender so first-launch
  ;     extraction of the portable runtime is not scanned file-by-file. Failure
  ;     (Defender disabled, group policy, no cmdlet) must not block the install.
  nsExec::ExecToLog 'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Add-MpPreference -ExclusionPath \"${PRODUCT_DATADIR}\" -ErrorAction SilentlyContinue"'
  Pop $0
  ; (b) The firewall allow rules are intentionally NOT created here. The images
  ;     that bind a non-loopback UDP socket (and thus trip the Windows Firewall
  ;     prompt) are the RTC agent's python.exe and livekit-server.exe under the
  ;     PER-USER %LOCALAPPDATA%\OpenDeskBotV2\bin\, which does not exist at
  ;     install time and whose end user may differ from the installing account
  ;     for an all-users install. The launcher provisions those two path-based
  ;     rules once (single UAC) on first run after it materializes the stable
  ;     bin dir; a rule on $INSTDIR\${PRODUCT_EXE} would match the wrong image
  ;     and never suppress the prompt. See OpenDeskBotV2Launcher.cs
  ;     FirewallRuleInstaller.
SectionEnd

Section "$(SEC_DESKTOP)" SecDesktop
  CreateShortcut "$DESKTOP\${PRODUCT_NAME}.lnk" "$INSTDIR\${PRODUCT_EXE}" "" "$INSTDIR\${PRODUCT_EXE}" 0
SectionEnd

Section /o "$(SEC_STARTUP)" SecStartup
  ; Opt-in auto-start: HKCU Run points at the installed EXE.
  WriteRegStr HKCU "${RUNKEY}" "${RUNVALUE}" "$\"$INSTDIR\${PRODUCT_EXE}$\""
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecCore}    "$(DESC_CORE)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "$(DESC_DESKTOP)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SecStartup} "$(DESC_STARTUP)"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ============================================================================
;  Init: single-instance installer + language selection
; ============================================================================
Function .onInit
  ; Only one installer instance at a time.
  System::Call 'kernel32::CreateMutex(p 0, i 0, t "${SETUP_MUTEX}") p .r1 ?e'
  Pop $0
  ${If} $0 <> 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(ALREADY_RUNNING)"
    Abort
  ${EndIf}

  ; 64-bit registry view for a $PROGRAMFILES64 install.
  SetRegView 64

  !insertmacro MUI_LANGDLL_DISPLAY
FunctionEnd

; ============================================================================
;  Uninstall
; ============================================================================
Function un.onInit
  SetRegView 64
  !insertmacro MUI_UNGETLANGUAGE
FunctionEnd

Section "Uninstall"
  ; Stop the client (and its child services) before removing files.
  nsExec::Exec 'taskkill /F /T /IM "${PRODUCT_EXE}"'
  Pop $0
  Sleep 500

  ; Reverse host hardening (best-effort, silent). The firewall rules are the
  ; two path-based rules the launcher created on first run (names must match
  ; OpenDeskBotV2Launcher.cs LiveKitRuleName / PythonRuleName); the legacy
  ; single-name rule is deleted too so upgrades from older installs are clean.
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="${FW_RULE_NAME} (LiveKit SFU)"'
  Pop $0
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="${FW_RULE_NAME} (Python runtime)"'
  Pop $0
  nsExec::ExecToLog 'netsh advfirewall firewall delete rule name="${FW_RULE_NAME}"'
  Pop $0
  nsExec::ExecToLog 'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Remove-MpPreference -ExclusionPath \"${PRODUCT_DATADIR}\" -ErrorAction SilentlyContinue"'
  Pop $0

  ; Shortcuts.
  Delete "$SMPROGRAMS\${PRODUCT_NAME}.lnk"
  Delete "$DESKTOP\${PRODUCT_NAME}.lnk"

  ; Auto-start entry (only present if the component was selected).
  DeleteRegValue HKCU "${RUNKEY}" "${RUNVALUE}"

  ; Program files.
  Delete "$INSTDIR\${PRODUCT_EXE}"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  ; Add/Remove Programs entry.
  DeleteRegKey HKLM "${UNINSTKEY}"

  ; Ask before touching user data; default is No (keep it).
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "$(DATA_PROMPT)" IDNO KeepData
    RMDir /r "${PRODUCT_DATADIR}"
  KeepData:
SectionEnd
