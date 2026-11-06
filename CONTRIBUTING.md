# 参与贡献

## 环境

- Python 3.12
- Node.js 20+
- Windows（桌面端打包需要；只跑后端/前端测试在 Linux/macOS 也可以）

## 跑起来

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev,qwen,grade,web]"
.venv\Scripts\python -m uvicorn web.server:app --port 8080

cd web-desktop2
npm install
npm run dev
```

## 提交前

```bash
.venv\Scripts\python -m pytest -q
cd web-desktop2
npm run typecheck
npm test
```

- 一个功能一个提交，提交信息用中文 Conventional Commits（如 `feat(plan): ...`）；
- 不要提交 `.env`、真实 API Key、`data/ecdict.csv`；
- 界面改动请附截图；行为改动请补测试。

## PR

- 说明改了什么、为什么、怎么验证；
- 保持改动聚焦，不混入无关格式化。
