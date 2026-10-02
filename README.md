# repro_exec

本地复现脚本执行库，由缺陷管理上层直接调用。执行记录与保留输出均在内存中。

## 用法

```python
from repro_exec import ReproLibrary

lib = ReproLibrary()
eid = lib.submit("BUG-123", "echo hi", "sh", timeout_seconds=10.0)  # 立即返回
lib.cancel(eid)                 # 请求取消，返回是否被接受
lib.get_execution(eid)          # 查看单次执行（ExecutionView 快照）
lib.get_latest("BUG-123")       # 查看某缺陷最新提交的执行
```

- 语言支持 `sh` 与 `python`；每次执行使用提交时的脚本文本。
- 终态为 `SUCCESS` / `FAILED` / `TIMEOUT` / `CANCELLED` 之一，发布后不可变。
- stdout/stderr 各保留前 64 KiB 原始字节，并标明是否截断。
- 子进程运行于独立会话，终态发布前整组（含子孙进程）被回收。

## 运行测试

```sh
python3 -m pytest tests/ -q
```
