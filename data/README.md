# 词典数据

- `dict.db`：由 [ECDICT](https://github.com/skywind3000/ECDICT) 的 CSV 构建的 SQLite 词典库，
  方便直接运行；许可证见 `LICENSE-ECDICT.txt`（MIT，Copyright (c) 2025 Linwei）。
- 需要重新构建时：`python scripts/build_dict_db.py`（需要 ECDICT 的 `ecdict.csv` 放在 `data/`）。
