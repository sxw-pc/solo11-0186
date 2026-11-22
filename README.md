# 塔机吊装作业采证台

吊物方案、塔机索具人员和现场起落钩三账联动，决定起吊放行、停工及完工。

Python 3.11+ / SQLite，合成数据离线运行。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m liftpermit demo --db demo.sqlite
python -m liftpermit report --db demo.sqlite
python -m liftpermit audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：plan-import、asset-import、work-import。每项导入格式为 `--db state.sqlite --file fixtures/<name>.json`。同源重复幂等，异内容复用事件 ID 则整批回滚。`audit` 在空库重放原始文件并比对业务状态。
