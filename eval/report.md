# 发票审核评测报告

运行模式：`replay`；样本：200 张。
实际响应来源：synthetic: 240。

本次使用了由合成发票真值构造的 replay 夹具。抽取准确率反映夹具与真值的一致性，
不能视为 Claude 的真实抽取能力，也不能用作 live 实测成绩。规则、PDF 读取、路由、
人工恢复、数据库入账和尾差台账均通过完整流程运行。

| 指标 | 结果 |
| --- | --- |
| 问题票检出 | 40/40 (100.00%) |
| 正常票 blocking 误报 | 0/160 |
| 首次字段抽取准确率 | 8840/8840 (100.00%) |
| 首次路由准确率 | 200/200 (100.00%) |
| 尾差记账正确数 | 20/20 (100.00%) |
| 直通率 | 135/200 (67.50%) |
| 平均处理耗时（不含人工等待） | 61.16 ms |
| 处理错误 | 0 |

问题票检出要求该票标签中的全部异常码均被检出；误报只统计正常票的 blocking 异常。
字段按头字段和所有明细叶子字段计数，金额使用 Decimal 比较，文本使用 NFKC 与去首尾空格比较。
路由取首次待办或直通路线，包含 FIELD_VERIFY。耗时累计每次提交与恢复调用，不含人工思考或等待。

| 异常码 | 应检出 | 检出 | 漏报 | 标签外检出 |
| --- | ---: | ---: | ---: | ---: |
| AMOUNT_OVER_TOLERANCE | 18 | 18 | 0 | 0 |
| ARITHMETIC_ERROR | 5 | 5 | 0 | 0 |
| BUYER_MISMATCH | 5 | 5 | 0 | 0 |
| DUPLICATE_INVOICE | 5 | 5 | 0 | 0 |
| GR_ALREADY_INVOICED | 2 | 2 | 0 | 0 |
| GR_NOT_FOUND | 2 | 2 | 0 | 0 |
| LINE_NOT_ON_PO | 0 | 0 | 0 | 0 |
| LOW_CONFIDENCE | 0 | 0 | 0 | 0 |
| MISSING_FIELD | 0 | 0 | 0 | 0 |
| PO_NOT_FOUND | 0 | 0 | 0 | 0 |
| PRICE_MISMATCH | 6 | 6 | 0 | 0 |
| QTY_MISMATCH | 7 | 7 | 0 | 0 |
| TAX_RATE_MISMATCH | 4 | 4 | 0 | 0 |
| VENDOR_NOT_APPROVED | 4 | 4 | 0 | 0 |
| VENDOR_PO_MISMATCH | 0 | 0 | 0 | 0 |

CI 门禁：通过。
