# 历史档案

这里保存已完成阶段的计划、设计与整理前的验收记录，正文保留原阶段口径，不能作为当前配置或部署状态。当前入口见[文档导航](../README.md)。

| 日期 | 材料 | 后续对应说明 |
|---|---|---|
| 2026-10-24 | [数据来源与发布修复计划](2026-10-24/repair-plan.md) | [单数据集快照](../snapshot-release.md)、[数据政策](../../DATA_POLICY.md) |
| 2026-10-24 | [固定版本消费设计](2026-10-24/consumer-shadow-design.md)、[实施计划](2026-10-24/consumer-shadow-plan.md) | [消费端契约](../consumer-integration.md) |
| 2026-10-24 | [历史估值设计](2026-10-24/history-valuation-design.md)、[实施计划](2026-10-24/history-valuation-plan.md) | [历史与独立估值](../history-valuation.md) |
| 整理于 2026-10-25 | [原维护与验收记录](2026-10-25-maintenance-notes.md) | [维护与恢复](../operations.md) |

主要后续变化：登记范围由 11 个指数扩为 12 个，来源许可从私人缓存扩至登记的公开 Release；独立收盘价与估值已有 schema 1.3 消费路径，正式累计基线已发布。历史中的禁止发布约束、旧运行时刻和旧测试数量不自动适用于这些后续变化。
