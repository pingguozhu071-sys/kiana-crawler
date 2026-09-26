# -*- coding: utf-8 -*-
"""生成 NSIS 安装/卸载向导语言体系（v2.17.1 C4；v2.17.1.1 修正：LangString 用十进制语言 ID）。

产出：
  installer/langs/EnglishBritish.nlf + .nsh      （自建：NSIS 官方无英式语言文件）
  installer/langs/ChineseHongKong.nlf + .nsh     （自建：官方仅 TradChinese(TW)）
  installer/lang_strings.nsi                     （8 语种 × 18 条自定义文案 LangString）

运行：python tools/gen_nsis_langs.py
（依赖本机 NSIS 的官方 English.nsh / TradChinese.nsh 作为内置文案基底——见 NSIS_DIR 探测）

注意：NSIS 语言 ID 宏（LANG_ENGLISH 等）在 WinLang.nsh，且无 2057/3076 条目；
LangString 一律写十进制 ID，杜绝 "${LANG_XXX}" 未定义宏落入 2052 的错绑。
"""
import io
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "installer" / "langs"
STRINGS_OUT = ROOT / "installer" / "lang_strings.nsi"

# ── 文案键：MUI 页/卸载页/自定义文本变量 ─────────────────────────────
KEYS = [
    "K_ABORT",           # 中途退出警告
    "K_WELCOME_TITLE", "K_WELCOME_TEXT",
    "K_DIR_TOP", "K_DIR_BOTTOM",
    "K_FINISH_TITLE", "K_FINISH_TEXT", "K_RUN_LABEL",
    "K_SECTION_MAIN", "K_LNK_START", "K_LNK_UNINSTALL",
    "K_UNCONFIRM_TITLE", "K_UNCONFIRM_TEXT",
    "K_UNFINISH_TITLE", "K_UNFINISH_TEXT",
    "K_BRANDING", "K_HEADER_FEATURES", "K_VERSION_NOTE",
]

# ── 翻译表（8 语种；简洁正常版：安装引导，不做特性推广）──────────────
T = {
"en-GB": {
"K_ABORT": "Are you sure you want to quit the Kiana Vnext Plus installer?",
"K_WELCOME_TITLE": "Welcome to Kiana Vnext Plus",
"K_WELCOME_TEXT": "This wizard will install Kiana Vnext Plus on your computer.$\r$\n$\r$\nPlease close all other programs before continuing.$\r$\n$\r$\nClick Next to continue.",
"K_DIR_TOP": "Choose the folder in which to install Kiana Vnext Plus.",
"K_DIR_BOTTOM": "To install in the default location, click Install. Click Browse to choose a different folder.",
"K_FINISH_TITLE": "Installation Complete",
"K_FINISH_TEXT": "Kiana Vnext Plus has been installed.$\r$\n$\r$\nA shortcut has been placed on your desktop and in the Start menu.$\r$\n$\r$\nClick Finish to exit.",
"K_RUN_LABEL": "Launch Kiana now",
"K_SECTION_MAIN": "Kiana Vnext Plus (required)",
"K_LNK_START": "Kiana",
"K_LNK_UNINSTALL": "Uninstall Kiana Vnext Plus",
"K_UNCONFIRM_TITLE": "Uninstall Kiana Vnext Plus",
"K_UNCONFIRM_TEXT": "This will remove Kiana Vnext Plus from your computer.$\r$\n$\r$\nThe uninstaller removes the application files and Start-menu shortcuts; your crawl data, cookies and settings are kept.$\r$\n$\r$\nClick Uninstall to continue.",
"K_UNFINISH_TITLE": "Uninstall Complete",
"K_UNFINISH_TEXT": "Kiana Vnext Plus has been uninstalled.$\r$\n$\r$\nThank you for using Kiana.",
"K_BRANDING": "Kiana Labs",
"K_HEADER_FEATURES": "Data collection engine",
"K_VERSION_NOTE": "v2.17.1",
},
"en-US": {
"K_ABORT": "Are you sure you want to quit the Kiana Vnext Plus installer?",
"K_WELCOME_TITLE": "Welcome to Kiana Vnext Plus",
"K_WELCOME_TEXT": "This wizard will install Kiana Vnext Plus on your computer.$\r$\n$\r$\nPlease close all other programs before continuing.$\r$\n$\r$\nClick Next to continue.",
"K_DIR_TOP": "Choose the folder in which to install Kiana Vnext Plus.",
"K_DIR_BOTTOM": "To install in the default location, click Install. Click Browse to choose a different folder.",
"K_FINISH_TITLE": "Installation Complete",
"K_FINISH_TEXT": "Kiana Vnext Plus has been installed.$\r$\n$\r$\nA shortcut has been placed on your desktop and in the Start menu.$\r$\n$\r$\nClick Finish to exit.",
"K_RUN_LABEL": "Launch Kiana now",
"K_SECTION_MAIN": "Kiana Vnext Plus (required)",
"K_LNK_START": "Kiana",
"K_LNK_UNINSTALL": "Uninstall Kiana Vnext Plus",
"K_UNCONFIRM_TITLE": "Uninstall Kiana Vnext Plus",
"K_UNCONFIRM_TEXT": "This will remove Kiana Vnext Plus from your computer.$\r$\n$\r$\nThe uninstaller removes the application files and Start-menu shortcuts; your crawl data, cookies and settings are kept.$\r$\n$\r$\nClick Uninstall to continue.",
"K_UNFINISH_TITLE": "Uninstall Complete",
"K_UNFINISH_TEXT": "Kiana Vnext Plus has been uninstalled.$\r$\n$\r$\nThank you for using Kiana.",
"K_BRANDING": "Kiana Labs",
"K_HEADER_FEATURES": "Data collection engine",
"K_VERSION_NOTE": "v2.17.1",
},
"zh-CN": {
"K_ABORT": "确定要退出 Kiana Vnext Plus 安装程序吗？",
"K_WELCOME_TITLE": "欢迎使用 Kiana Vnext Plus",
"K_WELCOME_TEXT": "本向导将把 Kiana Vnext Plus 安装到您的计算机。$\r$\n$\r$\n请先关闭其它程序，然后再继续。$\r$\n$\r$\n点击「下一步」继续。",
"K_DIR_TOP": "选择 Kiana Vnext Plus 的安装位置。",
"K_DIR_BOTTOM": "点击「安装」安装到默认位置；点击「浏览」选择其它文件夹。",
"K_FINISH_TITLE": "安装完成",
"K_FINISH_TEXT": "Kiana Vnext Plus 已安装完成。$\r$\n$\r$\n桌面与开始菜单已创建快捷方式。$\r$\n$\r$\n点击「完成」退出安装程序。",
"K_RUN_LABEL": "立即启动 Kiana",
"K_SECTION_MAIN": "Kiana Vnext Plus（必需）",
"K_LNK_START": "Kiana",
"K_LNK_UNINSTALL": "卸载 Kiana Vnext Plus",
"K_UNCONFIRM_TITLE": "卸载 Kiana Vnext Plus",
"K_UNCONFIRM_TEXT": "这将从您的电脑中移除 Kiana Vnext Plus。$\r$\n$\r$\n卸载程序会删除应用程序文件与开始菜单快捷方式；您的爬取数据、cookies 与设置将被保留。$\r$\n$\r$\n点击「卸载」继续。",
"K_UNFINISH_TITLE": "卸载完成",
"K_UNFINISH_TEXT": "Kiana Vnext Plus 已卸载。$\r$\n$\r$\n感谢使用 Kiana。",
"K_BRANDING": "Kiana Labs",
"K_HEADER_FEATURES": "数据采集引擎",
"K_VERSION_NOTE": "v2.17.1",
},
"zh-TW": {
"K_ABORT": "確定要退出 Kiana Vnext Plus 安裝程式嗎？",
"K_WELCOME_TITLE": "歡迎使用 Kiana Vnext Plus",
"K_WELCOME_TEXT": "本安裝精靈將把 Kiana Vnext Plus 安裝到您的電腦。$\r$\n$\r$\n請先關閉其它程式，然後再繼續。$\r$\n$\r$\n點擊「下一步」繼續。",
"K_DIR_TOP": "選擇 Kiana Vnext Plus 的安裝位置。",
"K_DIR_BOTTOM": "點擊「安裝」安裝至預設位置；點擊「瀏覽」選擇其它資料夾。",
"K_FINISH_TITLE": "安裝完成",
"K_FINISH_TEXT": "Kiana Vnext Plus 已安裝完成。$\r$\n$\r$\n桌面與開始功能表已建立捷徑。$\r$\n$\r$\n點擊「完成」結束安裝程式。",
"K_RUN_LABEL": "立即啟動 Kiana",
"K_SECTION_MAIN": "Kiana Vnext Plus（必要）",
"K_LNK_START": "Kiana",
"K_LNK_UNINSTALL": "解除安裝 Kiana Vnext Plus",
"K_UNCONFIRM_TITLE": "解除安裝 Kiana Vnext Plus",
"K_UNCONFIRM_TEXT": "這將從您的電腦中移除 Kiana Vnext Plus。$\r$\n$\r$\n解除安裝程式會刪除應用程式檔案與開始功能表捷徑；您的爬取資料、cookies 與設定將被保留。$\r$\n$\r$\n點擊「解除安裝」繼續。",
"K_UNFINISH_TITLE": "解除安裝完成",
"K_UNFINISH_TEXT": "Kiana Vnext Plus 已解除安裝。$\r$\n$\r$\n感謝使用 Kiana。",
"K_BRANDING": "Kiana Labs",
"K_HEADER_FEATURES": "資料採集引擎",
"K_VERSION_NOTE": "v2.17.1",
},
"zh-HK": {
"K_ABORT": "確定要離開 Kiana Vnext Plus 安裝程式嗎？",
"K_WELCOME_TITLE": "歡迎使用 Kiana Vnext Plus",
"K_WELCOME_TEXT": "本安裝精靈會將 Kiana Vnext Plus 安裝到您的電腦。$\r$\n$\r$\n請先關閉其他程式，然後再繼續。$\r$\n$\r$\n按「下一步」繼續。",
"K_DIR_TOP": "選擇 Kiana Vnext Plus 的安裝位置。",
"K_DIR_BOTTOM": "按「安裝」安裝至預設位置；按「瀏覽」選擇其他資料夾。",
"K_FINISH_TITLE": "安裝完成",
"K_FINISH_TEXT": "Kiana Vnext Plus 已安裝完成。$\r$\n$\r$\n桌面與開始功能表已建立捷徑。$\r$\n$\r$\n按「完成」結束安裝程式。",
"K_RUN_LABEL": "立即啟動 Kiana",
"K_SECTION_MAIN": "Kiana Vnext Plus（必要）",
"K_LNK_START": "Kiana",
"K_LNK_UNINSTALL": "解除安裝 Kiana Vnext Plus",
"K_UNCONFIRM_TITLE": "解除安裝 Kiana Vnext Plus",
"K_UNCONFIRM_TEXT": "這會從您的電腦移除 Kiana Vnext Plus。$\r$\n$\r$\n解除安裝程式會刪除應用程式檔案與開始功能表捷徑；您的爬取數據、cookies 與設定將獲保留。$\r$\n$\r$\n按「解除安裝」繼續。",
"K_UNFINISH_TITLE": "解除安裝完成",
"K_UNFINISH_TEXT": "Kiana Vnext Plus 已解除安裝。$\r$\n$\r$\n多謝使用 Kiana。",
"K_BRANDING": "Kiana Labs",
"K_HEADER_FEATURES": "數據採集引擎",
"K_VERSION_NOTE": "v2.17.1",
},
"ja-JP": {
"K_ABORT": "Kiana Vnext Plus のインストーラーを終了しますか？",
"K_WELCOME_TITLE": "Kiana Vnext Plus へようこそ",
"K_WELCOME_TEXT": "このウィザードは、Kiana Vnext Plus をこのコンピューターにインストールします。$\r$\n$\r$\n続行する前に、他のアプリケーションをすべて閉じてください。$\r$\n$\r$\n「次へ」をクリックして続行してください。",
"K_DIR_TOP": "Kiana Vnext Plus のインストール先フォルダーを選択してください。",
"K_DIR_BOTTOM": "既定の場所にインストールする場合は「インストール」をクリックしてください。別のフォルダーを選択するには「参照」をクリックしてください。",
"K_FINISH_TITLE": "インストールの完了",
"K_FINISH_TEXT": "Kiana Vnext Plus のインストールが完了しました。$\r$\n$\r$\nデスクトップとスタートメニューにショートカットを作成しました。$\r$\n$\r$\n「完了」をクリックして終了してください。",
"K_RUN_LABEL": "Kiana を今すぐ起動",
"K_SECTION_MAIN": "Kiana Vnext Plus（必須）",
"K_LNK_START": "Kiana",
"K_LNK_UNINSTALL": "Kiana Vnext Plus をアンインストール",
"K_UNCONFIRM_TITLE": "Kiana Vnext Plus のアンインストール",
"K_UNCONFIRM_TEXT": "Kiana Vnext Plus をこのコンピューターから削除します。$\r$\n$\r$\nアンインストーラーはアプリケーションファイルとスタートメニューのショートカットを削除します。収集済みデータ・Cookie・設定は保持されます。$\r$\n$\r$\n「アンインストール」をクリックして続行してください。",
"K_UNFINISH_TITLE": "アンインストールの完了",
"K_UNFINISH_TEXT": "Kiana Vnext Plus はアンインストールされました。$\r$\n$\r$\nご利用ありがとうございました。",
"K_BRANDING": "Kiana Labs",
"K_HEADER_FEATURES": "データ収集エンジン",
"K_VERSION_NOTE": "v2.17.1",
},
"ko-KR": {
"K_ABORT": "Kiana Vnext Plus 설치 프로그램을 종료하시겠습니까?",
"K_WELCOME_TITLE": "Kiana Vnext Plus 설치 마법사",
"K_WELCOME_TEXT": "이 마법사는 Kiana Vnext Plus를 컴퓨터에 설치합니다.$\r$\n$\r$\n계속하기 전에 다른 프로그램을 모두 닫아 주십시오.$\r$\n$\r$\n「다음」을 클릭하여 계속하십시오.",
"K_DIR_TOP": "Kiana Vnext Plus를 설치할 폴더를 선택하십시오.",
"K_DIR_BOTTOM": "기본 위치에 설치하려면 「설치」를 클릭하십시오. 다른 폴더를 선택하려면 「찾아보기」를 클릭하십시오.",
"K_FINISH_TITLE": "설치 완료",
"K_FINISH_TEXT": "Kiana Vnext Plus 설치가 완료되었습니다.$\r$\n$\r$\n바탕 화면과 시작 메뉴에 바로 가기를 만들었습니다.$\r$\n$\r$\n「완료」를 클릭하여 종료하십시오.",
"K_RUN_LABEL": "Kiana 지금 실행",
"K_SECTION_MAIN": "Kiana Vnext Plus (필수)",
"K_LNK_START": "Kiana",
"K_LNK_UNINSTALL": "Kiana Vnext Plus 제거",
"K_UNCONFIRM_TITLE": "Kiana Vnext Plus 제거",
"K_UNCONFIRM_TEXT": "컴퓨터에서 Kiana Vnext Plus를 제거합니다.$\r$\n$\r$\n제거 프로그램은 응용 프로그램 파일과 시작 메뉴 바로 가기를 삭제합니다. 수집한 데이터·쿠키·설정은 유지됩니다.$\r$\n$\r$\n「제거」를 클릭하여 계속하십시오.",
"K_UNFINISH_TITLE": "제거 완료",
"K_UNFINISH_TEXT": "Kiana Vnext Plus가 제거되었습니다.$\r$\n$\r$\n이용해 주셔서 감사합니다.",
"K_BRANDING": "Kiana Labs",
"K_HEADER_FEATURES": "데이터 수집 엔진",
"K_VERSION_NOTE": "v2.17.1",
},
"fr-FR": {
"K_ABORT": "Voulez-vous vraiment quitter l'installateur de Kiana Vnext Plus ?",
"K_WELCOME_TITLE": "Bienvenue dans Kiana Vnext Plus",
"K_WELCOME_TEXT": "Cet assistant va installer Kiana Vnext Plus sur votre ordinateur.$\r$\n$\r$\nVeuillez fermer tous les autres programmes avant de continuer.$\r$\n$\r$\nCliquez sur « Suivant » pour continuer.",
"K_DIR_TOP": "Choisissez le dossier d'installation de Kiana Vnext Plus.",
"K_DIR_BOTTOM": "Pour installer à l'emplacement par défaut, cliquez sur « Installer ». Pour choisir un autre dossier, cliquez sur « Parcourir ».",
"K_FINISH_TITLE": "Installation terminée",
"K_FINISH_TEXT": "Kiana Vnext Plus a été installé.$\r$\n$\r$\nUn raccourci a été créé sur le bureau et dans le menu Démarrer.$\r$\n$\r$\nCliquez sur « Terminer » pour quitter.",
"K_RUN_LABEL": "Lancer Kiana maintenant",
"K_SECTION_MAIN": "Kiana Vnext Plus (requis)",
"K_LNK_START": "Kiana",
"K_LNK_UNINSTALL": "Désinstaller Kiana Vnext Plus",
"K_UNCONFIRM_TITLE": "Désinstaller Kiana Vnext Plus",
"K_UNCONFIRM_TEXT": "Kiana Vnext Plus sera supprimé de votre ordinateur.$\r$\n$\r$\nLe désinstallateur supprime les fichiers de l'application et les raccourcis du menu Démarrer ; vos données collectées, cookies et paramètres sont conservés.$\r$\n$\r$\nCliquez sur « Désinstaller » pour continuer.",
"K_UNFINISH_TITLE": "Désinstallation terminée",
"K_UNFINISH_TEXT": "Kiana Vnext Plus a été désinstallé.$\r$\n$\r$\nMerci d'avoir utilisé Kiana.",
"K_BRANDING": "Kiana Labs",
"K_HEADER_FEATURES": "Moteur de collecte de données",
"K_VERSION_NOTE": "v2.17.1",
},
}

# ── 语言 ID（十进制；无需 WinLang.nsh 宏——NSIS 无 2057/3076 宏条目）──
LANG_IDS = {
    "en-GB": 2057, "en-US": 1033, "zh-CN": 2052, "zh-TW": 1028,
    "zh-HK": 3076, "ja-JP": 1041, "ko-KR": 1042, "fr-FR": 1036,
}

# ── 语言元信息（LangFile 第一个参数=NLFID）───────────────────────────
LANGS = {
    "ENGLISHBRITISH": ("EnglishBritish", "English (British)", "English (UK)", "Anglais (UK)", 2057),
    "CHINESEHONGKONG": ("ChineseHongKong", "Chinese (Traditional, HK)", "繁體中文（港澳）", "Chinois (HK)", 3076),
}


def _nlf_from_template(tpl_nlf, langid, name_native, name_en):
    """以官方 nlf 为基底（含完整 ^Xxx 内置字符串表）+ 替换语言 ID/名称行。

    教训 1：仅生成头字段的残缺 nlf 会被 NSIS 判为无效语言，静默回退默认语言。
    教训 2：makensis（Unicode 构建）只剥一层 BOM；重复加 BOM 会形成双 BOM
    （EF BB BF ×2），头部 "NLF v" 校验失败 → "Invalid language file"。
    教训 3：含中文的 nlf 若无 BOM，makensis 按系统 ACP（GBK）读 → "Bad text encoding"。
    规范：与官方一致——单 BOM；读出 utf-8-sig 剥掉后不再手工补，写入 utf-8-sig 负责加唯一一层。
    """
    import re as _re
    txt = io.open(tpl_nlf, encoding="utf-8-sig").read()
    # 语言 ID（# Language ID 下一行）。替换模板用 lambda：
    # "\\1" + 数字会拼成 \1208… 被 re 误读组号，吞掉 "# Language ID" 行（出 P57 事故）
    txt = _re.sub(r'(# Language ID\r?\n)(\d+)',
                  lambda m: m.group(1) + str(langid), txt, count=1)
    # 语言名（# Language Name 下一行）
    txt = _re.sub(r'(# Language Name\r?\n)([^\r\n]+)',
                  lambda m: m.group(1) + name_native, txt, count=1)
    # 英语言名（# English Language Name 下一行——某些模板没有该标记，需容错）
    m = _re.search(r'(# English Language Name\r?\n)([^\r\n]+)', txt)
    if m:
        txt = txt[:m.start(1)] + m.group(1) + name_en + txt[m.end(2):]
    return txt


def gen_lang_pair(tpl_nlf, tpl_nsh, dict_overrides, meta):
    nid, name_en, name_native, translator, langid = meta
    (OUT_DIR / f"{nid}.nlf").write_text(
        _nlf_from_template(tpl_nlf, langid, name_native, name_en),
        encoding="utf-8-sig")  # 单 BOM：官方 nlf 同款（makensis 恰好剥一层）
    # nsh：以官方同名文件为基底（内置 MUI 字符串），仅替换首两行注释与 LANGFILE 名
    txt = io.open(tpl_nsh, encoding="utf-8-sig").read()
    txt = txt.replace(";Language:", f";Language: {name_native} ({langid})", 1)
    import re as _re
    txt = _re.sub(r'!insertmacro LANGFILE "[^"]+"',
                  f'!insertmacro LANGFILE "{nid}"', txt, count=1)
    if dict_overrides:
        for k, v in dict_overrides.items():
            txt = txt.replace(k, v)
    (OUT_DIR / f"{nid}.nsh").write_text(txt, encoding="utf-8-sig")
    print(f"lang pair {nid} ok ({langid})")


def gen_strings():
    lines = ["; Kiana 自定义文案（8 语种；生成器 tools/gen_nsis_langs.py 管理——勿手改）",
             "; LanString 一律使用十进制语言 ID（2057/3076 无宏；防止错绑 2052）", ""]
    for lang, langid in LANG_IDS.items():
        lines.append(f"; ----- {lang} ({langid}) -----")
        for key in KEYS:
            # dict 值里 "$\r$\n" 经 Python 求值变成真实 CR/LF——输出前还原为 NSIS
            # 字面转义序列（CR 与 LF 不相邻，需分别替换；否则 NSIS 报 unterminated string）
            val = T[lang][key].replace("\r", r"\r").replace("\n", r"\n")
            lines.append(f'LangString {key} {langid} "{val}"')
        lines.append("")
    STRINGS_OUT.write_text("\n".join(lines), encoding="utf-8-sig")
    print("strings file ok:", STRINGS_OUT.name, "entries:", len(KEYS) * len(LANG_IDS))


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    nsis_dir = os.environ.get("NSIS_DIR") or r"C:\Program Files (x86)\NSIS"
    lf_dir = os.path.join(nsis_dir, "Contrib", "Language files")
    # nlf/nsh 都以官方文件为基底：nlf 保留完整 ^Xxx 字符串表（残缺会被 NSIS 判无效语言）
    gen_lang_pair(os.path.join(lf_dir, "English.nlf"),
                  os.path.join(lf_dir, "English.nsh"), None,
                  LANGS["ENGLISHBRITISH"])
    gen_lang_pair(os.path.join(lf_dir, "TradChinese.nlf"),
                  os.path.join(lf_dir, "TradChinese.nsh"), {
                      "Chinese (Traditional)": "Chinese (Traditional, HK)",
                  },
                  LANGS["CHINESEHONGKONG"])
    gen_strings()


if __name__ == "__main__":
    main()
