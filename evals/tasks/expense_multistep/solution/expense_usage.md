# 记账小工具使用说明

用 add_record 添加记录，total 计算总额，by_name 按名称汇总。

```python
from expense import add_record, total, by_name

records = []
add_record(records, "午饭", 20)
print(total(records))
```
