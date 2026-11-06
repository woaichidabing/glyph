"""pytest 公共夹具与配置。

src 布局的导入路径由 pyproject.toml 的 [tool.pytest.ini_options] pythonpath=["src"]
统一加入，不在此手工改 sys.path。

收集任何测试前先加载项目根 .env，让 integration 测试能读到 DASHSCOPE_API_KEY；
普通单测仍全程不联网（铁律 R6）。
"""

from glyph.config import load_dotenv

load_dotenv()


import os  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

# 凭据文件必须隔离：2026-10-28 起 save_provider_book 会把明文钥匙迁进凭据库，
# 不隔离的话跑测试就会往开发机真实的 ~/.markr/credentials.json 里塞测试密钥。
_CREDENTIALS_DIR = Path(tempfile.mkdtemp(prefix="markr-test-credentials-"))
os.environ.setdefault("GLYPH_CREDENTIALS", str(_CREDENTIALS_DIR / "credentials.json"))

# 提示词版本文件同样隔离：2026-10-31 起装配会读 ~/.markr/prompts 的当前版本，
# 不隔离的话，开发机切过的版本会让「说话风格」相关测试随人而异（还会被测试写入）。
_PROMPTS_DIR = Path(tempfile.mkdtemp(prefix="markr-test-prompts-"))
os.environ.setdefault("GLYPH_PROMPTS", str(_PROMPTS_DIR))


@pytest.fixture(autouse=True, scope="session")
def _isolated_credentials_file():
    """整轮测试共用一份临时凭据文件，绝不碰用户真实凭据。"""
    return os.environ["GLYPH_CREDENTIALS"]
