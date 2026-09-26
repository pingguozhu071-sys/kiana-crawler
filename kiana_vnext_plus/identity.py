import ctypes
from pathlib import Path
from omegaconf import OmegaConf


def secure_erase(data: bytearray):
    """安全擦除内存中的敏感数据"""
    ctypes.memset((ctypes.c_char * len(data)).from_buffer(data), 0, len(data))


class ProjectIdentity:
    """项目身份管理：目录结构、加密密钥、项目配置"""

    def __init__(self, project_id: str, base_dir = Path("./projects"), ephemeral: bool = False):
        self.project_id = project_id
        self.ephemeral = ephemeral
        # 兼容字符串和 Path 类型的 base_dir
        self.dir = Path(base_dir) / project_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cookie_enc_file = self.dir / "cookies.enc"
        self.cache_dir = self.dir / "cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.log_dir = self.dir / "logs"
        self.log_dir.mkdir(exist_ok=True)
        self.export_dir = self.dir / "export"
        self.export_dir.mkdir(exist_ok=True)
        # [FIXED & MODIFIED] 媒体目录统一到 export 下——原 video_dir=根目录/videos 与
        # UniversalDownloader(export_dir) 不一致，造成"根目录空壳文件夹 + 真数据在 export"的乌龙
        self.video_dir = self.export_dir / "videos"
        self.image_dir = self.export_dir / "images"
        self.audio_dir = self.export_dir / "audio"
        for _d in (self.video_dir, self.image_dir, self.audio_dir):
            _d.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "frontier.db"
        self.config_file = self.dir / "project.yaml"

        if self.config_file.exists():
            self.config = OmegaConf.load(self.config_file)
        else:
            self.config = OmegaConf.create()

        defaults = OmegaConf.create({
            "privacy": {
                "cookie_encrypt": True,
                "log_sanitize": True,
                "dns_strategy": "doh",
                "proxy_group": None,
                "proxy_sticky": True,
            },
            "limits": {
                "max_depth": 8,
                "max_pages": 5000,
                "max_pages_per_domain": 5000,
                "rate_limit_global": 5,
                "rate_limit_per_domain": 20,  # [FIXED & MODIFIED] v2.10.5 P1-7 单域并发默认 2→20（配 20 核激进）
                "max_runtime_seconds": 3600,
                "max_duplicate_rate": 0.5,
            },
            "parsers": {"article_selector": ".article-body", "title_selector": "h1"},
            "export": {"format": "jsonl"},
            "use_browser": True,
            "video_settings": {
                "max_downloads_per_domain": 50,
                "preferred_resolution": "highest",
            },
        })

        self.config = OmegaConf.merge(defaults, self.config)

        # [FIXED & MODIFIED] v2.11 Fernet 全链删除：self.fernet/derive_fernet_key/
        # cookie_enc_file 全工程零消费者，KIANA_CRYPTO_KEY 环境变量唯一作用就是喂它。
        # 本机敏感数据落盘加密由 privacy_store.DPAPI 承担（master.key.bin）——
        # 原 KIANA_CRYPTO_KEY 缺失即 raise 的启动失败模式一并消除。
        self.config_file.write_text(OmegaConf.to_yaml(self.config))

    def get_db_path(self):
        return str(self.db_path)
