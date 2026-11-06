"""凭据存放（照 DeepSeek Harness 的 credentials seam 还原）。

为什么要有它：DSH 的配置文件里**只有凭据的名字**（apiKeyEnv），值另存一处；
这样配置文件可以随便给人看、进备份、贴到 issue 里都不泄密。我们之前把 key 直接写进
provider.json，和配置混在一起，是把这条最要紧的分界线抹掉了。

这条通道的规矩（与 DSH 一致）：
1. **写进去、读不出**：对外只回答"配没配、从哪来、能不能写"，绝不回明文；
2. 解析顺序：本文件 refs → 进程环境变量 → 项目根 .env → ~/.markr/.env（空串算没配）；
3. 文件权限 0600、原子写；坏文件不抛异常（读不出来就当没有，绝不阻断启动）。

它只管"名字 → 值"，不认识供应商、不认识模型——那一层在 glyph.provider。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..storage import data_root

__all__ = ["CredentialInfo", "CredentialStore", "credentials_path"]


def credentials_path() -> Path:
    """凭据文件位置：``~/.markr/credentials.json``（与 provider.json 平级）。"""
    import os

    raw = os.environ.get("GLYPH_CREDENTIALS", "").strip()
    if raw:
        return Path(raw).expanduser()
    return data_root() / "credentials.json"


@dataclass(frozen=True)
class CredentialInfo:
    """一个引用的状态：配没配、从哪来、能不能写。**从不含值本身**。"""

    configured: bool
    source: str  # "store" | "env" | "dotenv" | "missing"
    writable: bool


class CredentialStore:
    """本地凭据：名字 → 值。读写都走原子替换，坏文件按空处理。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or credentials_path()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, str]:
        import json

        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        refs = data.get("refs") if isinstance(data, dict) else None
        if not isinstance(refs, dict):
            return {}
        return {str(k): str(v) for k, v in refs.items() if isinstance(v, str) and v}

    def _save(self, refs: dict[str, str]) -> None:
        import json
        import os

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        payload = {"version": 1, "refs": refs}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass  # Windows 上没有 POSIX 权限位：不阻断（与 DSH 在 win32 的行为一致）
        os.replace(tmp, self._path)

    # ---- 对外三条：写 / 删 / 查状态 ----

    def set(self, ref: str, value: str) -> None:
        text = (value or "").strip()
        if not text:
            raise ValueError("凭据不能为空")
        refs = self._load()
        refs[ref] = text
        self._save(refs)

    def unset(self, ref: str) -> None:
        refs = self._load()
        if ref in refs:
            refs.pop(ref)
            self._save(refs)

    def describe(self, ref: str) -> CredentialInfo:
        writable = self._writable()
        if self._load().get(ref):
            return CredentialInfo(True, "store", writable)
        if _from_environ(ref):
            return CredentialInfo(True, "env", writable)
        if _from_dotenv(ref):
            return CredentialInfo(True, "dotenv", writable)
        return CredentialInfo(False, "missing", writable)

    def resolve(self, ref: str) -> str | None:
        """真正取值（只在发请求时用）：store → 环境变量 → .env；空串一律算没配。"""
        value = self._load().get(ref)
        if value:
            return value
        return _from_environ(ref) or _from_dotenv(ref)

    def _writable(self) -> bool:
        import os

        try:
            if self._path.exists():
                return os.access(self._path, os.W_OK)
            parent = self._path.parent if self._path.parent.exists() else self._path.parent.parent
            return os.access(parent, os.W_OK)
        except OSError:
            return False


def _from_environ(ref: str) -> str | None:
    import os

    value = os.environ.get(ref, "").strip()
    return value or None


def _from_dotenv(ref: str) -> str | None:
    """从项目根 .env 与 ~/.markr/.env 里找（只读，不注入环境）。"""
    from pathlib import Path

    candidates = [Path.cwd() / ".env", data_root() / ".env"]
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() != ref:
                continue
            cleaned = value.strip().strip(("\"", "\'"))
            if cleaned:
                return cleaned
    return None
