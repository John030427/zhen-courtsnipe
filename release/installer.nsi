; Per-user installer around the portable package that scripts/build_release.py assembles.
;   makensis -INPUTCHARSET UTF8 -DVERSION=<v> -DSRC=<package dir> -DOUTFILE=<Setup.exe> -DICON=<icon.ico> installer.nsi
; No admin rights: installs to %LOCALAPPDATA%\Programs, registers under HKCU. Program files only;
; config, order journal and logs stay in %LOCALAPPDATA%\SustechReservation\data and survive
; upgrades and uninstall.
Unicode true
SetCompressor /SOLID lzma
RequestExecutionLevel user
ManifestDPIAware true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

!define APPNAME "南科大场地预约工作台"
!define SHORTNAME "南科大场地预约"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\SustechReservation"
!define TASKS_RUNNING 3 ; launcher.py --stop --quiet: refused because a rush / watch is running

Name "${APPNAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\SustechReservation"
InstallDirRegKey HKCU "${UNINSTKEY}" "InstallLocation"
BrandingText "${APPNAME} ${VERSION}"

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "将安装 ${APPNAME}（${VERSION}）。$\r$\n$\r$\n安装在你自己的用户目录里，不需要管理员权限。已经装过的话会直接升级，你的配置和订单记录不受影响。$\r$\n$\r$\n如果工作台正在运行，安装时会先让它退出。"
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "打开${SHORTNAME}"
!define MUI_FINISHPAGE_RUN_FUNCTION StartApp
!define MUI_FINISHPAGE_TEXT "桌面和开始菜单里有「${SHORTNAME}」。$\r$\n$\r$\n第一次使用：在开始菜单「${SHORTNAME}」文件夹里运行「配置自动获取登录」。"

!insertmacro MUI_PAGE_WELCOME
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE CheckNotBusy
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

; Ask a running workbench (of an earlier install) to exit so its files can be replaced.
; $0 = launcher exit code; 3 means a rush or watch is running and nothing was stopped.
!macro StopWorkbench FORCE
  StrCpy $0 0
  ${If} ${FileExists} "$INSTDIR\runtime\pythonw.exe"
  ${AndIf} ${FileExists} "$INSTDIR\app\launcher.py"
    ExecWait '"$INSTDIR\runtime\pythonw.exe" "$INSTDIR\app\launcher.py" --stop --quiet ${FORCE}' $0
    Sleep 1500
  ${EndIf}
!macroend

Function CheckNotBusy
  !insertmacro StopWorkbench ""
  ${If} $0 == ${TASKS_RUNNING}
    MessageBox MB_ICONEXCLAMATION|MB_OK "工作台里有抢场或监控任务正在运行。$\r$\n请先在页面里停止任务（或点「退出服务」），再点「安装」。"
    Abort
  ${EndIf}
FunctionEnd

Function StartApp
  SetOutPath "$INSTDIR"
  Exec '"$INSTDIR\runtime\pythonw.exe" "$INSTDIR\app\launcher.py"'
FunctionEnd

Section "Install"
  SetShellVarContext current
  ; Silent installs skip the directory page, so check again here.
  !insertmacro StopWorkbench ""
  ${If} $0 == ${TASKS_RUNNING}
    MessageBox MB_ICONEXCLAMATION|MB_OK "工作台里有抢场或监控任务正在运行，没有安装。请先停止任务再安装。" /SD IDOK
    SetErrorLevel ${TASKS_RUNNING}
    Quit
  ${EndIf}

  ; Old program files go first so a removed module cannot linger; user data is not under $INSTDIR.
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\runtime"
  SetOutPath "$INSTDIR"
  File /r "${SRC}/*"
  SetOutPath "$INSTDIR\app"
  File "/oname=icon.ico" "${ICON}"
  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; Shortcuts start in $INSTDIR (the current output path).
  CreateShortCut "$DESKTOP\${SHORTNAME}.lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\launcher.py"' "$INSTDIR\app\icon.ico"
  CreateDirectory "$SMPROGRAMS\${SHORTNAME}"
  CreateShortCut "$SMPROGRAMS\${SHORTNAME}\${SHORTNAME}.lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\launcher.py"' "$INSTDIR\app\icon.ico"
  CreateShortCut "$SMPROGRAMS\${SHORTNAME}\停止工作台.lnk" "$INSTDIR\runtime\pythonw.exe" '"$INSTDIR\app\launcher.py" --stop' "$INSTDIR\app\icon.ico"
  CreateShortCut "$SMPROGRAMS\${SHORTNAME}\配置自动获取登录.lnk" "$INSTDIR\配置自动获取.cmd" "" "$INSTDIR\app\icon.ico"
  CreateShortCut "$SMPROGRAMS\${SHORTNAME}\撤销自动获取配置.lnk" "$INSTDIR\撤销自动获取配置.cmd" "" "$INSTDIR\app\icon.ico"
  CreateShortCut "$SMPROGRAMS\${SHORTNAME}\使用说明.lnk" "$INSTDIR\使用说明.html"
  CreateShortCut "$SMPROGRAMS\${SHORTNAME}\其他代理软件配置说明.lnk" "$WINDIR\notepad.exe" '"$INSTDIR\docs\本地agent提示词.md"' 
  CreateShortCut "$SMPROGRAMS\${SHORTNAME}\卸载${SHORTNAME}.lnk" "$INSTDIR\uninstall.exe"

  ; 「设置 → 应用」 entry for this user.
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "内部自用"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\app\icon.ico"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINSTKEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $1 $2 $3
  WriteRegDWORD HKCU "${UNINSTKEY}" "EstimatedSize" $1
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  ; Auto-capture changes outlive the program: Clash would keep sending the booking site to
  ; 127.0.0.1:8080 with nothing listening, and the booking page would stop loading.
  ${If} ${FileExists} "$LOCALAPPDATA\SustechReservation\capture-setup\state.json"
    MessageBox MB_YESNO|MB_ICONQUESTION "你配置过自动获取登录（Clash 规则 / 本机证书）。$\r$\n卸载后工作台不在了，Clash 仍会把预约网站转到本机，预约页会打不开。$\r$\n$\r$\n现在先运行「撤销自动获取配置」吗？（推荐）" /SD IDNO IDNO keep_capture
    ExecWait '"$SYSDIR\cmd.exe" /c ""$INSTDIR\撤销自动获取配置.cmd""'
  keep_capture:
  ${EndIf}

  !insertmacro StopWorkbench ""
  ${If} $0 == ${TASKS_RUNNING}
    MessageBox MB_YESNO|MB_ICONEXCLAMATION "有抢场或监控任务正在运行，卸载会停止它们。继续吗？" /SD IDYES IDYES stop_tasks
    Abort
  stop_tasks:
    !insertmacro StopWorkbench "--force"
  ${EndIf}

  Delete "$DESKTOP\${SHORTNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${SHORTNAME}"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTKEY}"
  MessageBox MB_OK "已卸载。你的配置和订单记录还在：$\r$\n$LOCALAPPDATA\SustechReservation\data$\r$\n不再需要的话可以手动删除这个文件夹。" /SD IDOK
SectionEnd
