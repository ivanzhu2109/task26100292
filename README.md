# task26100292

## reprolib — 本地复现脚本执行库

缺陷管理上层直接调用的进程内库，无接口层、无外部服务，执行记录保存在内存中。

### API

```python
from reprolib import ReproExecutor, Language, Status

executor = ReproExecutor()

# 提交复现：立即返回执行标识，不等待脚本结束
eid = executor.submit(
    defect_id="BUG-123",
    script="echo hello",
    language=Language.SH,        # 或 Language.PYTHON
    timeout_seconds=30,          # 从提交被接受时开始计时
)

executor.cancel(eid)             # 取消执行（幂等）
executor.get_execution(eid)      # 查看单次执行（ExecutionSnapshot 或 None）
executor.get_latest("BUG-123")   # 查看某缺陷最新提交的执行
```

- 终态唯一且不可变：`SUCCESS` / `FAILURE` / `TIMEOUT` / `CANCELLED`。
- “最新执行”按提交被接受的顺序确定，与完成顺序无关，取消后不回退。
- stdout / stderr 各保留前 64 KiB 原始字节，并带 `truncated` 标记；
  超出的输出被丢弃但不会阻塞脚本。
- 公布终态前，主脚本及其进程组内的全部后代都会被停止
  （先 SIGTERM，宽限后 SIGKILL）。

### 运行全部测试

```sh
python3 -m pytest
```
