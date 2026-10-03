; Kiana Vnext Plus installer (NSIS 3.x) — v2.17.2 现代风格 + 8 语种安装/卸载向导（P0: 装/卸 Section 修复 + 字体清晰化 + DPI 感知）
; 美术对齐 GUI：暗色渐变 + #4FA3E8 强调 + 8px 圆角卡片；字体 Roboto（System 注册，回退系统）
; 语言：en-GB(默认回退)/en-US/zh-CN/zh-TW/zh-HK/ja-JP/ko-KR/fr-FR；自定义文案 lang_strings.nsi
Unicode True

!define APP_NAME "Kiana Vnext Plus"
!define APP_EXE "KianaLauncher.exe"
!define APP_VERSION "2.19.9"
!define VERSION "2.19.9.0"
!define PUBLISHER "Kiana Labs"
!define DIST_DIR "dist"
!define ASSETS_DIR "assets"
!define LANGS_DIR "installer/langs"

Name "${APP_NAME}"
OutFile "KianaVnextPlus-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\KianaVnextPlus"
InstallDirRegKey HKCU "Software\KianaVnextPlus" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 64

VIProductVersion "${VERSION}"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "LegalCopyright" "(c) ${PUBLISHER}"
VIAddVersionKey "OriginalFilename" "KianaVnextPlus-Setup.exe"
Icon "${ASSETS_DIR}\icon.ico"

; ---------- MUI2 美术/语言定制（文案全部按语言变量 $() 运行时解析）----------
!define MUI_ICON "${ASSETS_DIR}\icon.ico"
!define MUI_UNICON "${ASSETS_DIR}\icon.ico"
; [P1 修复 v2.17.2] 字体清晰化：
;  ① 删 MUI_FONT_1/2 死定义（MUI2 无此宏，Roboto 注册了也没消费者）
;  ② ManifestDPIAware true：高分屏不再被 DWM 位图拉伸（模糊主因之二）
;  ③ SetFont 宋体(SimSun) 10pt：用户指名标准宋体；9pt 宋体走点阵渲染发糊，
;     10pt+ 走 TrueType 轮廓清晰（按语言分别设置：中文宋体/日韩文自带/西文 Segoe UI）
ManifestDPIAware true
SetFont /LANG=2052 "SimSun" 10
SetFont /LANG=1028 "MingLiU" 10
SetFont /LANG=3076 "MingLiU" 10
SetFont /LANG=1041 "MS PGothic" 10
SetFont /LANG=1042 "Gulim" 10
SetFont /LANG=1033 "Segoe UI" 10
SetFont /LANG=2057 "Segoe UI" 10
SetFont /LANG=1036 "Segoe UI" 10
!define MUI_ABORTWARNING
!define MUI_ABORTWARNING_TEXT "$(K_ABORT)"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_BITMAP "${__FILEDIR__}\assets\installer\header.bmp"
!define MUI_HEADERIMAGE_RIGHT
!define MUI_WELCOMEFINISHPAGE_BITMAP "${__FILEDIR__}\assets\installer\welcome.bmp"
; [v2.18.1] 卸载向导专属侧图（深空玻璃同语言，"移除/数据保留"语义）
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "${__FILEDIR__}\assets\installer\unwelcome.bmp"
!define MUI_BRANDINGTEXT "$(K_BRANDING)"
!define MUI_WELCOMEPAGE_TITLE "$(K_WELCOME_TITLE)"
!define MUI_WELCOMEPAGE_TEXT "$(K_WELCOME_TEXT)"
!define MUI_DIRECTORYPAGE_TEXT_TOP "$(K_DIR_TOP)"
!define MUI_DIRECTORYPAGE_TEXT_BOTTOM "$(K_DIR_BOTTOM)"
!define MUI_FINISHPAGE_TITLE "$(K_FINISH_TITLE)"
!define MUI_FINISHPAGE_TEXT "$(K_FINISH_TEXT)"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "$(K_RUN_LABEL)"
!define MUI_FINISHPAGE_NOREBOOTSUPPORT
; 卸载向导定制（此前全默认——页名/文案/提示目录与"数据保留"说明）
!define MUI_UNCONFIRMPAGE_TITLE "$(K_UNCONFIRM_TITLE)"
!define MUI_UNCONFIRMPAGE_TEXT_TOP "$(K_UNCONFIRM_TEXT)"
!define MUI_UNFINISHPAGE_TITLE "$(K_UNFINISH_TITLE)"
!define MUI_UNFINISHPAGE_TEXT "$(K_UNFINISH_TEXT)"
; 语言选择：系统 UI 语言自动预选 + 可手动选；未匹配 → 第一语言 en-GB
!define MUI_LANGDLL_DISPLAY

!include "MUI2.nsh"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; ---------- 语言注册（en-GB 首位=默认回退；zh-HK 与 en-GB 为自建语言段）----------
; NSIS 官方无这两个语言的 LANG_ 宏——手动补齐（MUI_LANGUAGEEX 内部引用 ${LANG_<NLFID>}）
!define LANG_EnglishBritish 2057
!define LANG_ChineseHongKong 3076
!insertmacro MUI_LANGUAGEEX "${LANGS_DIR}" "EnglishBritish"
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "TradChinese"
!insertmacro MUI_LANGUAGE "Japanese"
!insertmacro MUI_LANGUAGE "Korean"
!insertmacro MUI_LANGUAGE "French"
!insertmacro MUI_LANGUAGE "English"
!insertmacro MUI_LANGUAGEEX "${LANGS_DIR}" "ChineseHongKong"

!include "installer\lang_strings.nsi"

; ---------- 安装逻辑（v2.16 起 onedir 双 exe + Chromium 随包）----------
Section "$(K_SECTION_MAIN)" SEC_MAIN
    SectionIn RO
    SetOutPath "$INSTDIR"
    ; [v2.18 P2-11] 升级覆盖防护：双 onedir 合并共享 $INSTDIR\_internal（当前 12,483
    ; 同名文件逐字节一致）。升级时 NSIS 只覆盖不清理——旧版残留 DLL 与新版混跑
    ; （PyInstaller 升级换依赖版本后必炸）。重装/升级前先清 _internal（首次安装无此目录，零影响）。
    RMDir /r "$INSTDIR\_internal"
    File /r "${DIST_DIR}\KianaLauncher\*.*"
    File /r "${DIST_DIR}\KianaCrawler\*.*"
    File "${ASSETS_DIR}\icon.ico"
    File "${ASSETS_DIR}\cover.png"
    ; 字体随包（GUI 启动时 AddFontResource 使用；安装向导 .onInit 已临时注册）
    SetOutPath "$INSTDIR\fonts"
    File "${ASSETS_DIR}\fonts\Roboto-Regular.ttf"
    SetOutPath "$INSTDIR"

    CreateShortCut "$DESKTOP\Kiana.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\icon.ico"
    CreateDirectory "$SMPROGRAMS\Kiana Vnext Plus"
    CreateShortCut "$SMPROGRAMS\Kiana Vnext Plus\$(K_LNK_START).lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\icon.ico"
    CreateShortCut "$SMPROGRAMS\Kiana Vnext Plus\$(K_LNK_UNINSTALL).lnk" "$INSTDIR\Uninstall.exe" "" "$INSTDIR\icon.ico"

    ; [v2.19.5 质检修复] 记录"上一个安装目录"。此前安装**无条件覆盖** InstallDir，
    ; 导致"安装第二个副本 → 卸载副本"时目录匹配条件成立 → 误删**原安装**的桌面快捷方式
    ; 与注册表（2026-09-13 实测复现）。现改为：装到与已登记目录**不同**的位置时，
    ; 把原值存入 PrevInstallDir，卸载副本时据此恢复原安装的快捷方式与注册表。
    ReadRegStr $0 HKCU "Software\KianaVnextPlus" "InstallDir"
    StrCmp "$0" "" kiana_no_prev
    StrCmp "$0" "$INSTDIR" kiana_no_prev
        WriteRegStr HKCU "Software\KianaVnextPlus" "PrevInstallDir" "$0"
        ; 同时记录原安装的版本号——卸载副本时需一并恢复，否则控制面板会显示副本的版本
        ReadRegStr $2 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KianaVnextPlus" \
            "DisplayVersion"
        WriteRegStr HKCU "Software\KianaVnextPlus" "PrevDisplayVersion" "$2"
    kiana_no_prev:
    WriteRegStr HKCU "Software\KianaVnextPlus" "InstallDir" "$INSTDIR"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KianaVnextPlus" \
        "DisplayName" "${APP_NAME}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KianaVnextPlus" \
        "DisplayIcon" "$INSTDIR\icon.ico"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KianaVnextPlus" \
        "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KianaVnextPlus" \
        "Publisher" "${PUBLISHER}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KianaVnextPlus" \
        "UninstallString" "$INSTDIR\Uninstall.exe"
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KianaVnextPlus" \
        "NoModify" 1
    WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KianaVnextPlus" \
        "NoRepair" 1
    WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

; ---------- 卸载器（[P0 修复 v2.17.2] 原 L123 把卸载逻辑写成无 un. 前缀的普通
; Section——MUI2 下必执行，装完 520MB 被 RMDir /r $INSTDIR 全删光，卸载器 0 Section
; 空壳。现转为真正的 un.Section，卸载器才有人干活）----------
Section "un.KianaVnextPlus"
    ; [v2.19 安全] 跨实例防护：桌面快捷方式与开始菜单目录是**产品级共享资源**。
    ; [v2.19.5 质检修复] 完整逻辑（前两版各有缺口，均经实测复现后重写）：
    ;   安装时会写 PrevInstallDir（若装到与已登记目录不同的位置）。
    ;   卸载时三分支：
    ;     ① 本安装**不是**登记安装（$INSTDIR != InstallDir）→ 什么都不动
    ;        （旧版遗留情形：不动别人的快捷方式与注册表）
    ;     ② 是登记安装 且 存在有效的 PrevInstallDir → **恢复**原安装
    ;        （重建快捷方式 + 写回注册表登记），本副本的痕迹清除
    ;     ③ 是登记安装 且 无可恢复的原安装 → 彻底清理（快捷方式 + 注册表全删）
    ;   注意：②与③互斥——早期版本曾在"恢复"之后又走到"删除"分支，
    ;   把刚恢复的注册表抹掉（实测发现），故此处用 Goto 明确分岔。
    ReadRegStr $0 HKCU "Software\KianaVnextPlus" "InstallDir"
    ReadRegStr $1 HKCU "Software\KianaVnextPlus" "PrevInstallDir"
    StrCmp "$INSTDIR" "$0" 0 kiana_un_done

    ; ---- 分支②③共用：清理本安装创建的共享资源 ----
    Delete "$DESKTOP\Kiana.lnk"
    RMDir /r "$SMPROGRAMS\Kiana Vnext Plus"

    ; ---- 判断能否恢复原安装 ----
    StrCmp "$1" "" kiana_un_purge
    IfFileExists "$1\${APP_EXE}" 0 kiana_un_purge

    ; ---- 分支②：恢复原安装 ----
    WriteRegStr HKCU "Software\KianaVnextPlus" "InstallDir" "$1"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KianaVnextPlus" \
        "DisplayName" "${APP_NAME}"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KianaVnextPlus" \
        "DisplayIcon" "$1\icon.ico"
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KianaVnextPlus" \
        "UninstallString" "$1\Uninstall.exe"
    ; 版本号：用安装时记录的原值（否则会残留副本的版本，控制面板显示错误）
    ReadRegStr $2 HKCU "Software\KianaVnextPlus" "PrevDisplayVersion"
    StrCmp "$2" "" kiana_un_ver_skip
        WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KianaVnextPlus" \
            "DisplayVersion" "$2"
    kiana_un_ver_skip:
    ; [v2.19.8 实测修复] 快捷方式的"起始位置(WorkingDirectory)"取的是 **$OUTDIR**，而卸载段
    ; 此刻 $OUTDIR 仍停在**本副本的 $INSTDIR**（测试副本时= %TEMP%\kiana_mat_*\app）。
    ; 于是"恢复原安装"重建出来的桌面/开始菜单快捷方式：目标与图标都对，唯独起始位置指向
    ; 那个随后会被删掉的临时目录（实测复现：WorkDir = %TEMP%\kiana_mat_zh-CN_xxx\app\）。
    ; 安装段是在 SetOutPath "$INSTDIR" 之后才建快捷方式，所以没有这个问题——卸载段必须
    ; 显式切到"被恢复的安装目录"。
    SetOutPath "$1"
    CreateShortCut "$DESKTOP\Kiana.lnk" "$1\${APP_EXE}" "" "$1\icon.ico"
    CreateDirectory "$SMPROGRAMS\Kiana Vnext Plus"
    CreateShortCut "$SMPROGRAMS\Kiana Vnext Plus\$(K_LNK_START).lnk" "$1\${APP_EXE}" "" "$1\icon.ico"
    CreateShortCut "$SMPROGRAMS\Kiana Vnext Plus\$(K_LNK_UNINSTALL).lnk" "$1\Uninstall.exe" "" "$1\icon.ico"
    DeleteRegValue HKCU "Software\KianaVnextPlus" "PrevInstallDir"
    DeleteRegValue HKCU "Software\KianaVnextPlus" "PrevDisplayVersion"
    Goto kiana_un_done

    ; ---- 分支③：无可恢复 → 彻底清理 ----
    kiana_un_purge:
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\KianaVnextPlus"
    DeleteRegKey HKCU "Software\KianaVnextPlus"

    kiana_un_done:
    ; 字体会话注册随卸载器进程退出自动失效；数据目录(下载产物)按用户意愿手动清
    RMDir /r "$INSTDIR"
    ; [v2.18] 自清理收尾：Uninstall.exe 运行中无法删除自身（此前实测总残留这 1 个
    ; 文件）——脱手后由 cmd 延迟 2s 整目录扫尾（标准 NSIS 手法；黑窗一闪即逝属预期）
    Exec '"$WINDIR\System32\cmd.exe" /C ping 127.0.0.1 -n 3 > nul & rmdir /S /Q "$INSTDIR"'
SectionEnd

; ---------- 字体注册（会话级 AddFontResource；卸载器同）----------
Function .onInit
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File "/oname=Roboto-Regular.ttf" "${ASSETS_DIR}\fonts\Roboto-Regular.ttf"
    System::Call 'gdi32::AddFontResourceW(w "$PLUGINSDIR\Roboto-Regular.ttf") i .r0'
FunctionEnd

Function un.onInit
    ; 字体注册为会话级（AddFontResourceW），卸载器独立进程无需 Remove（系统重启自然清）
FunctionEnd
