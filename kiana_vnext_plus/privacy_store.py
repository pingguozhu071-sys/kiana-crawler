"""DPAPI 加密存储（v2.11 隐私拉满）

Windows CryptProtectData/CryptUnprotectData（ctypes，无第三方依赖）：
- 密钥由当前 Windows 用户账户持有——本机不可移植性即隐私特性（拷到别的机器无法解密）
- 用于 master_password 等本机敏感数据的落盘加密（原明文 master.key）
"""
import os
import ctypes
import ctypes.wintypes
import pathlib
import logging

logger = logging.getLogger(__name__)

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))


def _from_blob(blob: _DATA_BLOB) -> bytes:
    out = ctypes.string_at(blob.pbData, blob.cbData)
    ctypes.windll.kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))
    return out


def dpapi_protect(data: bytes) -> bytes:
    """加密（当前用户可解；其他用户/机器无法解）"""
    if os.name != "nt":
        raise RuntimeError("DPAPI 仅 Windows 可用")
    out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(_blob(data)), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError(f"CryptProtectData failed: {ctypes.GetLastError()}")
    return _from_blob(out)


def dpapi_unprotect(data: bytes) -> bytes:
    """解密（非本机/本用户将抛 OSError）"""
    if os.name != "nt":
        raise RuntimeError("DPAPI 仅 Windows 可用")
    out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(_blob(data)), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError(f"CryptUnprotectData failed: {ctypes.GetLastError()}")
    return _from_blob(out)


def save_protected(path: pathlib.Path, text: str) -> None:
    """文本 → DPAPI 加密落盘（.bin）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dpapi_protect(text.encode("utf-8")))


def load_protected(path: pathlib.Path) -> str:
    """DPAPI 加密文件 → 文本（不存在/解密失败抛异常）"""
    return dpapi_unprotect(path.read_bytes()).decode("utf-8")


# ─────────────────────────────────────────────────────────────
# [v2.19.7 安全·扫描发现] 结构化密钥（dict）的 DPAPI 落盘
#   动机：GUI「打码 API 密钥」等敏感 dict 此前**明文**写 launcher_config.json
#   （界面还宣称"DPAPI 加密"），任何本机进程/同步盘/备份都能直接读到。
#   这里给出通用「dict → JSON → DPAPI」原语：成功返回 True；DPAPI 不可用
#   （非 Windows / 调用失败）返回 False，**由调用方决定降级方式**——本函数
#   绝不静默改写为明文。
# ─────────────────────────────────────────────────────────────
def secret_path(name: str) -> pathlib.Path:
    """密钥文件路径：data_root()/secrets/<name>.bin（便携模式随 exe 走）"""
    from .config import data_root
    safe = "".join(c for c in str(name) if c.isalnum() or c in "_-") or "secret"
    return pathlib.Path(data_root()) / "secrets" / f"{safe}.bin"


def save_secret_json(name: str, obj) -> bool:
    """字典 → JSON → DPAPI 加密落盘。成功 True；DPAPI 不可用 False（调用方决定降级）"""
    import json as _json
    try:
        save_protected(secret_path(name), _json.dumps(obj, ensure_ascii=False))
        return True
    except Exception as e:
        logger.warning(f"DPAPI 加密保存 {name} 失败（调用方需决定降级策略）: {e}")
        return False


def load_secret_json(name: str):
    """DPAPI 密文 → dict。文件不存在/解密失败/内容非 JSON → None（不抛异常）"""
    import json as _json
    try:
        p = secret_path(name)
        if not p.exists():
            return None
        val = _json.loads(load_protected(p))
        return val if isinstance(val, dict) else None
    except Exception as e:
        logger.warning(f"DPAPI 解密读取 {name} 失败: {e}")
        return None


def delete_secret(name: str) -> bool:
    """删除密钥文件（清空密钥时用）。不存在视为成功"""
    try:
        secret_path(name).unlink(missing_ok=True)
        return True
    except Exception as e:
        logger.warning(f"删除密钥文件 {name} 失败: {e}")
        return False
