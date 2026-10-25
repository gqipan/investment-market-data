# Investment Market Data

可审计的指数行情与估值数据工具，负责采集、质量校验、内容寻址快照、累计发布和固定版本读取。Git 保存通用代码、目录配置、文档与合成样例；获准的真实市场数据通过 GitHub Release 分发。

## 数据用途声明

本项目整理的市场数据仅供个人学习与非商业的私人研究，不用于商业数据服务，不构成投资建议。数据权利归实际权利人所有，使用须遵守各来源条款。本项目不授予超出上游许可的商业使用或再分发权；代码 MIT 许可不覆盖数据。完整说明见[数据政策](DATA_POLICY.md)。

## 当前状态

截至 2026-10-25：

- 登记 12 个指数、25 个数据集定义（12 份价格、12 份估值、1 份合成样例）。登记数量不代表实际可得历史或发布快照数量。
- 首个正式、不可变累计基线 [data-2026-10-23-r1](https://github.com/gqipan/investment-market-data/releases/tag/data-2026-10-23-r1) 已发布，包含多来源快照；实际覆盖以该版本清单和覆盖报告为准。
- `daily index release` 每天北京时间 21:40、23:40、次日 09:00 检查未发布的已闭市交易日，补齐后追加累计包；无待发布会话时正常结束。
- `check` 在 push、PR、手动触发和每日 20:17 运行测试、隐私与目录检查、合成消费验算；`release candidate` 只生成合成候选。
- 当前使用来源已在 catalog 登记所有者的公开采集、保存和发布批准。代码许可、来源用途与私人业务数据边界见 [DATA_POLICY.md](DATA_POLICY.md)。

## 快速开始

从仓库根目录运行，需要 Python 3.11 或以上。核心校验仅依赖标准库；若本机只有 `python3`，将下例的 `python` 替换为 `python3`。

```bash
PYTHONPATH=src python -m market_data --help
PYTHONPATH=src python -m unittest discover -s tests
PYTHONPATH=src python -m market_data catalog-check --root .
PYTHONPATH=src python -m market_data privacy-check --path . --repository
```

需要 AkShare 采集接口时，先在虚拟环境中安装可选依赖：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install '.[collection]'
```

采集、回补与输出命令均保留既有结果；重跑时使用新目录。`data/`、`dist/` 与本地构建产物被 Git 忽略，但 `data/`、`dist/` 仍参与隐私内容扫描。完整开发与验证步骤见[开发维护](docs/development.md)。

## 选择操作入口

| 目标 | 文档 |
|---|---|
| 下载正式累计包、校验并生成研究缓存 | [每日累计发布与读取](docs/daily-release.md) |
| 回补行情与估值、理解 partial 和实际频率 | [历史与独立估值](docs/history-valuation.md) |
| 单数据集采集、候选打包与发布门禁 | [单数据集快照](docs/snapshot-release.md) |
| 理解指数身份、来源和历史门槛 | [指数与来源](docs/index-catalog.md) |
| 接入固定版本、影子对照与应用边界 | [消费端契约](docs/consumer-integration.md) |
| 检查运行结果、处理失败与恢复 | [维护与恢复](docs/operations.md) |
| 查阅全部文档和历史方案 | [文档导航](docs/README.md) |

每日累计包与单数据集包使用不同格式。累计包使用 `cumulative-manifest.json`，通过 `extract-cumulative`、`materialize-cumulative`、`verify-registry-cache` 读取；单数据集包使用 `release-manifest.json`，通过 `verify-release`、`stage-*` / `fetch-*` 读取。

## 数据边界

来源按数据集显式选择，禁止静默换源、拼接不同口径或填造 OHLC。完整 OHLCV、独立收盘序列与估值观测分别验证；创业板R 保留 `total_return_index` 身份。尚无完整 12 指数 PE/PB/股息率日历史，月度或当日观测继续保留其实际频率。

消费组件只安装和验证研究缓存，不切换生产指针、不导入私人业务数据库。私人应用中的持仓、账本、净值、报告和凭据不属于本项目输入。合成样例只能用于开发与验收，不能正式发布。

代码使用 MIT License；真实数据遵循实际来源的独立使用条件。
