# 架构

`api/` 负责请求和错误响应，调用审核服务。`graph/` 负责 LangGraph 节点、Send 并行分发、条件边和 interrupt；业务规则放在 `rules/` 与 `routing.py`。`services/` 封装 PDF、LLM、字段核对和人工动作。`repo/` 是 sqlite3 仓储层，负责 ERP 数据、占用、审计及最终事务。

## 金额与规则

业务模型使用 Pydantic v2。金额使用 Decimal，数据库用 TEXT 保存；不含税单价保留原始精度，行金额与合计用 ROUND_HALF_UP 舍入到分。策略来自 `config/policy.yaml`，不把模型输出用作结论。

规则使用只读 AuditContext，返回结构化 Finding。R01 失败时跳过 R03–R11；其余情况各规则独立执行。R11 的允许尾差不豁免 R03 的内部算术检查。路由纯函数按 blocking、review、金额阈值决定审核节点。

## 暂停与恢复

`run_id` 同时作为 LangGraph `thread_id`。检查点使用 SqliteSaver，业务数据与检查点分别保存在 `app.sqlite` 和 `checkpoints.sqlite`。`interrupt` 载荷与恢复请求先经过 Pydantic 模型校验，再以 JSON 形式写入检查点。字段核对和补交文档会清空上一轮 Finding、说明与尾差，重新加载上下文并审核。

API 在调用 Command(resume=...) 前校验待办、角色和动作。运行表的版本号与 CAS 锁阻止两个请求推进同一版本。无效请求不写入决定或改变运行状态。恢复执行中抛出的错误不会驳回发票：服务把恢复前的检查点复制为最新检查点（`update_state(..., as_node="__copy__")`），重新进入原 interrupt，恢复补交前的占用，并记录 `rolled_back` 审计事件。首次提交时的识别失败仍会以 REJECTED 结束并释放占用。

## 唯一性与事务

占用表为 active 的发票身份 `(seller_tax_id, invoice_number)` 和 GR 建部分唯一索引。占用变更在 BEGIN IMMEDIATE 事务中执行；同一运行重复占用有效，补交文档更换身份时释放旧占用。等待补材料保留占用，驳回和取消释放。补材料次数用尽后，`wait_for_info` 不再暂停，直接以 CANCELLED 结束。

批准事务重新核对身份与 GR 是否由其他运行持有，然后写入应付台账、付款队列、尾差并标记 GR。`run_id` 唯一键和发票唯一索引保证幂等。人工审批不能绕过这些唯一性约束，任何写入失败都会回滚整个事务。

## LLM 与离线验证

LLMClient 的 live / record / replay 使用同一结构化接口，缓存键绑定任务、版本化提示词、模型、输入与 JSON Schema。replay 只读缓存，缺失时明确失败。合成夹具标记为 synthetic，真实录制标记为 recorded；评测报告区分来源。差异说明服务不可用或返回审批结论时，审核页使用规则自带的中文差异消息，并记录来源；说明失败不改变规则结论和人工待办。

合成生成器产出票据、ERP seed、真值及离线夹具。审核流程只读取 PDF、ERP 和缓存；标签只供评测比对和模拟人工操作。GitHub Actions 无 API Key 运行单测与 replay 评测。

Docker 使用单进程 Uvicorn 和非 root 用户。Compose 的 `/app/runtime` 卷保存业务库、检查点与上传文档；镜像包含只读使用的 ERP seed 和默认 replay 夹具，启动时不重新生成。
