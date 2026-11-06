"""read_file：读取用户上传到本机的文本文件（txt / md）。

- 只读（ReadOnlyTool）：不动文件，允许同批次并发；
- 安全边界：只能读装配层声明的「允许根目录」（上传目录）里的文本文件。
  解析后的真实路径越出根目录、或后缀不是文本类，一律拒绝，避免模型
  （或被注入的提示）拿它去读系统里的任意文件（如密钥、配置）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..runtime.errors import ToolError
from .base import ReadOnlyTool

__all__ = ["ReadFileTool"]

_TEXT_SUFFIXES = {".txt", ".md"}
# 与 web 装配层 _decode_text 保持同一套回退顺序
_ENCODINGS = ("utf-8", "gbk", "gb18030", "latin-1")


class ReadFileTool(ReadOnlyTool):
    name = "read_file"
    description = (
        "读取用户上传到本机的文本文件（.txt / .md）内容。"
        "当且仅当用户上传了文件、或给了你一个本机文件路径，而你需要看到里面写了什么时才调用；"
        "参数 path 用用户（或上传接口）给出的绝对路径，原样传入。"
        "只能读上传目录里的 txt/md，读别的路径或别的类型会被拒绝。"
        "不要用于：用户只是提到了某个文件但没让你读、或你自己猜一个路径去翻——"
        "路径只能来自用户或上传接口；它只读 txt/md 文本，读不了图片或其它二进制文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件绝对路径（用户上传后由系统给出）",
                "minLength": 1,
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        allowed_roots: list[Path] | tuple[Path, ...],
        *,
        max_chars: int = 20000,
    ) -> None:
        self._roots = [Path(r).expanduser().resolve() for r in allowed_roots]
        self._max_chars = max_chars

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_path = str(args.get("path", "")).strip()
        # 阻塞的文件 IO 放同步 helper（本地小文本，与 memory 工具同样处理）
        return self._read_sync(raw_path)

    def _read_sync(self, raw_path: str) -> dict[str, Any]:
        if not raw_path:
            raise ToolError("缺少要读取的文件路径（path）")
        try:
            path = Path(raw_path).expanduser().resolve()
        except (OSError, ValueError) as exc:
            raise ToolError(f"文件路径无法解析：{raw_path!r}") from exc

        if not path.is_file():
            raise ToolError(f"文件不存在或不是一个文件：{path}")
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            raise ToolError(f"只支持读取 .txt / .md 文本文件：{path.name}")
        if not any(self._is_within(path, root) for root in self._roots):
            raise ToolError("只能读取你上传目录里的文件，这个路径不在允许范围内。")

        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ToolError(f"读取文件失败：{exc}") from exc

        text = self._decode(data)
        if text is None:
            raise ToolError("文件不是可识别的文本编码（utf-8 / gbk / gb18030 均失败）")

        truncated = False
        if len(text) > self._max_chars:
            text = text[: self._max_chars]
            truncated = True
        return {
            "path": str(path),
            "content": text,
            "chars": len(text),
            "truncated": truncated,
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _decode(data: bytes) -> str | None:
        for enc in _ENCODINGS:
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return None
