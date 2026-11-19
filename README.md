# pairwise-cache-key

## 题面

缓存配置从单租户改成多租户后，边缘缓存开始串内容：query 按原始字符串参与哈希，参数顺序和重复键一变就生成新键，缓存被打散；百分号编码不归一，%41 与 A、+ 与 %20 各存一份；host 直接用 Host 头也不去尾随点，大小写变体和 a.example.com. 能污染别人的键；Vary 只看配置声明的头、不校验租户头，跨租户命中同一份缓存；并发时同一个 key 多个请求同时回源，结果互相覆盖。edgecache/keys.py 是核心，tests/test_keys.py 15个红。要修的是：query 归一后排序并保留重复项；百分号解码一次再规范重编码；host 小写去尾随点并做 IDNA；租户头必须存在且格式合法并进入键；同一 key 的并发回源合并成一次。

## 运行方式

在仓库根目录执行：

```bash
python -m pytest tests -q
```

初始失败环境必须恰好输出 `15 failed`，且不得出现 collection error、import error、syntax error 或 test error。修复后应全部通过。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 安全 |
| 子方向 | 漏洞检测与攻防 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## tests 不可修改

`tests/test_keys.py` 是验收契约，包含且仅包含 15 个测试。不要修改测试文件或测试断言来让测试通过；修复应发生在 `edgecache/keys.py`（5 个桩函数与 `InvalidKey` 异常类）。
