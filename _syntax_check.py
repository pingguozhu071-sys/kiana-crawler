"""语法检查脚本 - 由一键构建.bat 调用

⚠️ [v2.19.8 标注・已被取代] `一键构建.bat` 的 [4/7] 步已改为**内联** `python -c` 语法检查，
入口对齐现行 `launcher_v9.py`（本文件仍 glob `launcher_v8.py`，注释里那句"v1 的
kiana_gui/build 目录已不存在"说的就是这类历史漂移）。**当前没有任何调用点**。
要手动做语法检查请用：
    python -c "import py_compile,glob; [py_compile.compile(f, doraise=True) for f in ['launcher_v9.py','run_crawler.py']+glob.glob('kiana_vnext_plus/*.py')]; print('OK')"
保留原因：仅作历史记录。"""
import py_compile
import glob
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# [FIXED & MODIFIED] v2.10.5 对齐双入口（v1 的 kiana_gui/build 目录已不存在）
files = (
    ["launcher_v8.py", "run_crawler.py"]
    + glob.glob("kiana_vnext_plus/*.py")
)

errors = []
for f in files:
    try:
        py_compile.compile(f, doraise=True)
    except py_compile.PyCompileError as e:
        errors.append((f, str(e)))

if errors:
    print("  X 语法检查失败:")
    for f, e in errors:
        print(f"    {f}: {e[:100]}")
    sys.exit(1)
else:
    print(f"  OK 语法检查通过: {len(files)} 个文件")
    sys.exit(0)
