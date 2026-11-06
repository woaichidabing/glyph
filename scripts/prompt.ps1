# 提示词版本管理的一键入口（把 python -m glyph.prompt_store 包一层）
# 用法：
#   .\scripts\prompt.ps1 list
#   .\scripts\prompt.ps1 new 少问一句 --note 别老追问
#   .\scripts\prompt.ps1 use v2
#   .\scripts\prompt.ps1 diff v1 v2
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Args2
)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

# 中文输出按 UTF-8 打，避免 GBK 控制台把中文显示成乱码
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = Join-Path $root 'src'

& $python -m glyph.prompt_store @Args2
exit $LASTEXITCODE
