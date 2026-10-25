# 开发维护

## 目录职责

```text
catalog/                 指数身份、来源许可、数据集定义、采集范围
src/market_data/         Python CLI 与采集、校验、打包、消费组件
tests/                   标准库 unittest 测试
fixtures/                明确标记的合成样例
.github/workflows/       check、release candidate、daily index release
docs/                    当前操作与契约说明
docs/archive/            按日期保留的历史计划和验收
data/                    本地真实快照、请求缓存与覆盖报告（忽略）
dist/                    本地候选、报告和下载产物（忽略）
```

`catalog/watchlist.json` 是本项目公开指数采集范围，不能写入私人关注原因。真实数据不进入 Git；本地 `build/`、`*.egg-info/`、虚拟环境和测试缓存均是生成产物。历史快照和请求缓存是恢复与审计依据，整理目录时保留，清理前核对是否已有独立归档。

## 模块分工

| 模块 | 职责 |
|---|---|
| `catalog.py` | 配置结构、身份与来源引用、目录报告 |
| `collection.py`、`history.py`、`valuation.py` | 严格单快照、批量历史回补、独立估值观测 |
| `akshare_worker.py`、`valuation_worker.py` | 在独立子进程执行可选 AkShare 接口 |
| `quality.py` | 数值、OHLC、日期与独立交易日历验证 |
| `release.py`、`general_release.py` | 单数据集候选、schema 1.2 / 1.3 验算与门禁 |
| `release_window.py`、`cumulative.py` | 闭市会话范围、累计基线与增量、材料化和缓存复验 |
| `consumer.py`、`transport.py`、`shadow.py` | 固定身份安装、匿名下载与只读影子对照 |
| `privacy.py`、`provenance.py` | 隐私正文扫描、仓库文件白名单、Git 来源校验 |
| `cli.py` | 命令参数、输出与退出码 |

## 本地验收

需要 Python 3.11 或以上。从仓库根目录运行；核心测试不需要 AkShare 或在线上游。下面使用 `python`，本机命令为 `python3` 时替换即可。

```bash
PYTHONPATH=src python -m unittest discover -s tests
PYTHONPATH=src python -m market_data catalog-check --root .
PYTHONPATH=src python -m market_data privacy-check --path . --repository
git diff --check
```

`privacy-check --repository` 同时检查已跟踪与未忽略文件的发布白名单。忽略规则不等于内容扫描豁免；未知文件、环境文件、符号链接、私人字段和凭据模式可能导致拒绝。完整检查会扫描本地数据，因此耗时随 `data/` 与 `dist/` 增长。

合成候选往返（输出目录须尚不存在，重跑时更换 `docs-smoke`）：

```bash
PYTHONPATH=src python -m market_data build-candidate \
  --dataset csi300-demo --as-of 2026-10-22 --revision 1 \
  --output dist/docs-smoke
PYTHONPATH=src python -m market_data verify-release \
  --release-dir dist/docs-smoke/data-2026-10-22-fixture-r1
PYTHONPATH=src python -m market_data catalog-report \
  --root . --output-dir dist/docs-smoke/catalog
```

合成包不能通过正式 `release-check`。通过本地检查不代表远程 CI 已运行；需要核对与具体 commit 对应的成功运行。

## CLI 索引

精确参数以 `PYTHONPATH=src python -m market_data <命令> --help` 为准。

| 目标 | 命令 | 说明 |
|---|---|---|
| 配置与隐私 | `catalog-check`、`catalog-report`、`privacy-check` | 目录验证、报告和仓库扫描 |
| 采集与回补 | `collect`、`backfill`、`verify-valuation` | [历史与估值](history-valuation.md)、[单快照](snapshot-release.md) |
| 单数据集候选 | `build-candidate`、`build-snapshot-release`、`verify-release`、`release-check` | [单数据集快照](snapshot-release.md) |
| 累计包 | `build-cumulative`、`extract-cumulative`、`materialize-cumulative`、`verify-delta-tree`、`verify-registry-cache` | [每日累计发布与读取](daily-release.md) |
| 固定版本消费 | `stage-release`、`fetch-release`、`stage-snapshot-release`、`fetch-snapshot-release`、`shadow-check` | [消费端契约](consumer-integration.md) |

`release_window` 另有模块入口：`PYTHONPATH=src python -m market_data.release_window --help`。它使用独立交易日历计算下一次补缺区间。

## 维护文档

[文档导航](README.md)列出各文档的核对依据。功能、来源映射、权限、包格式或工作流变更时同步更新对应文档；避免在多个位置重复维护长覆盖表。阶段验收注明日期和证据；旧计划转入档案并保留原文，当前限制写入操作文档。
