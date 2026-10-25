# 文档导航

当前说明核对日期：2026-10-25。入口为[项目 README](../README.md)，下面按操作目标查阅。

## 当前文档

| 文档 | 维护内容 | 核对依据 |
|---|---|---|
| [开发维护](development.md) | 目录职责、命令索引、本地验收与文档维护约定 | `src/market_data/`、`tests/`、`pyproject.toml` |
| [指数与来源](index-catalog.md) | 指数身份、显式选源、历史门槛 | `catalog/*.json`、采集适配器 |
| [历史与独立估值](history-valuation.md) | 回补操作、质量状态、字段方法与有日期的覆盖结果 | `history.py`、`valuation.py`、每次运行的覆盖报告 |
| [单数据集快照](snapshot-release.md) | collect、schema 1.2 / 1.3 候选与发布门禁 | `collection.py`、`release.py`、`general_release.py` |
| [每日累计发布与读取](daily-release.md) | 定时、基线、增量、累计包下载与材料化 | `daily-release.yml`、`release_window.py`、`cumulative.py` |
| [消费端契约](consumer-integration.md) | 固定身份消费、研究缓存与生产边界 | `consumer.py`、`transport.py`、`shadow.py` |
| [维护与恢复](operations.md) | 当前验收证据、常见失败、恢复边界与未完成事项 | 实际 commit、Actions、Release 与本地验证 |
| [数据政策](../DATA_POLICY.md) | Git 与 Release 的数据边界、公开和私人范围 | `catalog/sources.json`、发布门禁 |
| [来源审批清单](source-approval-checklist.md) | 来源登记流程、状态与权限字段 | `catalog/sources.json`、发布门禁 |

## 历史材料

[历史档案索引](archive/README.md)保留旧计划、设计和阶段验收。历史正文中的“11 指数”“仅本地”“尚未发布”等描述属于当时范围，不覆盖当前说明。旧测试数与旧覆盖结果不应抄作当前验收结论。

## 更新规则

行为变化时同步修改上表对应文档，README 仅保留当前状态和入口。涉及数据范围和质量的结论必须注明请求区间、来源、数据类型与观测频率；发布与远程验收必须记录 tag、commit 或 run URL。以当前代码、catalog、工作流和实际产物为依据，历史记录只用于追溯。

新增文档须接入本索引；归档须标明日期并更新链接。私人消费端的部署状态由其所属项目核验，本项目只承诺自身代码与接口契约。
