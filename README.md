# Investment Market Data

可审计的市场数据采集、校验与打包工具。通用代码可以公开托管，真实数据是否发布按实际来源单独判断；指数编制方不等于必须直连的采集接口。公开仓库仅包含通用代码、配置和合成样例，真实快照只保存在本机。

## 当前可用能力

- 11 个指数已登记独立身份、价格与估值数据集。
- 价格默认使用腾讯原始日线 `tencent_raw`，无需中证官网；930955 没有可用腾讯映射，目录已改登记为 `akshare_eastmoney` 并保留“线上可取性待复核”状态。
- `backfill` 批量回补历史和独立估值，输出逐指数覆盖、频率、缺日与错误报告。
- 本地 `collect` 命令生成独立快照，包含行情、独立交易日历、实际来源、单位、日期、SHA-256 和内容身份。
- 支持合成样例或真实本地快照生成候选包；验算检查文件白名单、压缩包、隐私、哈希、行数和交易日覆盖。
- 完整 OHLCV、独立收盘价和估值观测现在都有固定身份 Release 路径；投资系统消费器会按调用方预先锁定的清单哈希、数据集 ID 和数据类型复验后安装。
- `release-check` 额外检查当前来源权限、真实 Git commit、代码一致性、干净工作树和仓库文件白名单。合成数据始终不能正式发布。
- 每日 Actions 只做测试、隐私和 catalog 检查，不进行未获准的云端取数或上传真实数据。

2026-10-24 已实际回补多年历史：腾讯10指数取得34,654条行情，2个完整OHLCV通过，8个分离为独立收盘序列（其中3个仍有缺日）；930955原登记行情源不可用。乐咕取得3指数的月度PE/PB历史，蛋卷取得9指数当日PE/PB/股息率快照；可显式采集独立的中证历史PE对照字段。尚无完整11指数估值日历史，不自动拼接、补值或发布。详见[历史与估值的运行、查看及实际覆盖](docs/history-valuation.md)。

## 本地检查

核心功能只需 Python 3.11，无第三方运行依赖：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m market_data privacy-check --path . --repository
PYTHONPATH=src python -m market_data catalog-check --root .
```

`--repository` 对 Git 将发布的文件执行白名单检查；内容扫描也检查 `data/`、`dist/` 和安全解包后的正文。目录名不再是豁免条件。二进制、环境文件、符号链接、额外发布文件和可识别的账本字段会被拒绝。自动扫描不能证明任意文本没有隐私；项目只能接受公开市场数据及通用代码，不能从私人应用或笔记复制文件。

## 采集真实行情

```bash
PYTHONPATH=src python -m market_data collect \
  --dataset csi300-normalized-v1 \
  --start 2026-10-16 --end 2026-10-23 \
  --output data/local/csi300-tencent-20261023
```

输出目录必须尚不存在，并位于仓库外或被忽略的 `data/` 内。不会覆盖既有快照。腾讯接口必须返回原始 `day` 序列，缺失时失败，不改用 `qfqday`。交易日历独立来自深交所，要求每个月完整，再核对请求区间的行情缺日和额外日期；不会从行情日期反推日历。

可选 AkShare 接口：

```bash
python -m pip install '.[collection]'
PYTHONPATH=src python -m market_data collect \
  --dataset csi300-normalized-v1 --provider akshare_eastmoney \
  --start 2026-10-16 --end 2026-10-23 \
  --output data/local/csi300-akshare-em-20261023
```

显式选择来源，不自动回退或拼接。腾讯每个请求最多尝试三次，每次超时 15 秒；AkShare 在独立进程内执行，整体超时 120 秒。AkShare 是接口库，清单仍记录实际东方财富端点和库版本。不同来源生成不同 `dataset_key`，不能当作同一历史序列。

本地研究采集、私人云保存和公开再分发分别登记。当前实际使用的腾讯、东方财富、独立交易日历、乐咕、蛋卷和中证对照来源已批准本人控制的私人服务器缓存，公开下载和再分发仍关闭。真实数据不提交 Git、不上传公开 Release；私人系统通过内容寻址快照和登记读取。运行数据仓库 Actions 时，只有明确工作流和来源许可才允许云端取数。

## 打包与验算

真实本地快照：

```bash
PYTHONPATH=src python -m market_data build-candidate \
  --dataset csi300-normalized-v1 \
  --snapshot data/local/csi300-tencent-20261023 \
  --as-of 2026-10-23 --revision 1 --output dist
PYTHONPATH=src python -m market_data verify-release \
  --release-dir dist/data-2026-10-23-tencent_raw-r1
```

独立收盘价或估值观测：

```bash
PYTHONPATH=src python -m market_data build-snapshot-release \
  --dataset csi300-normalized-v1 \
  --snapshot data/local/history-run/000300/price \
  --as-of 2026-10-23 --revision 1 --output dist
PYTHONPATH=src python -m market_data verify-release \
  --release-dir dist/data-2026-10-23-tencent_raw-r1

PYTHONPATH=src python -m market_data build-snapshot-release \
  --dataset csi300-valuation-v1 \
  --snapshot data/local/history-run/000300/akshare_legulegu \
  --as-of 2026-10-23 --revision 1 --output dist
PYTHONPATH=src python -m market_data verify-release \
  --release-dir dist/data-2026-10-23-akshare_legulegu-r1
```

合成测试样例：

```bash
PYTHONPATH=src python -m market_data build-candidate \
  --dataset csi300-demo --as-of 2026-10-22 --revision 1 --output dist
PYTHONPATH=src python -m market_data verify-release \
  --release-dir dist/data-2026-10-22-fixture-r1
```

候选清单使用 schema 1.2，旧版合成候选需重新构建。候选目录仅包含 `release-manifest.json`、`quality-report.json`、`SHA256SUMS` 和一个压缩包；包内仅允许同一目录中的 `bars.csv`、`calendar.csv`、`manifest.json`、`README.txt`。保留来源精度，不强制将价格四舍五入到两位。候选已通过验算不等于允许正式发布。

## 私人应用接入

已提供两套匿名固定版本消费路径：

- 完整 OHLCV：`stage-release` / `fetch-release`，生成国内研究 CLI 所需的 `market=A` 兼容缓存。
- 独立收盘价与估值：`stage-snapshot-release` / `fetch-snapshot-release`，原样保留 schema、来源、方法、单位和频率。

调用方必须锁定不可变 tag、清单哈希、数据集 ID 和数据类型。`shadow-check` 对照固定行情缓存，默认要求至少 900 个交易日；六日验收样本不能当作 850 日均线历史。数据仓库命令本身不切换生产指针或写业务数据库。消费命令与边界见[消费端接入契约](docs/consumer-integration.md)。

## 发布边界

```bash
PYTHONPATH=src python -m market_data release-check \
  --root . --release-dir dist/data-2026-10-23-tencent_raw-r1 --scope public
```

当前真实来源未获公开发布批准，因此上例应拒绝。工作树有修改时，自动记录的版本为 `local-uncommitted`，也不能正式发布。价格与独立日历来源分别核查。发布门禁会重新读取当前来源开关，撤销权限后旧候选同样失效；不能只相信候选里的旧批准标记。

同一份真实候选若只用于项目所有者控制的私人服务器，可以用 `--scope private` 验算后安装到私人缓存；私有通过不等于公开授权。

代码公开、数据公开、仓库当前可见性分别管理。本项目不会因为指数属于中证，就强制将整个代码仓库改成私有；使用 AkShare 也不自动给数据授予再分发权。来源身份、使用条件和发布范围见 [数据政策](DATA_POLICY.md) 与 [来源清单](catalog/sources.json)。

- [指数与来源说明](docs/index-catalog.md)
- [来源审批清单](docs/source-approval-checklist.md)
- [维护与验收记录](docs/operations.md)

代码使用 MIT License；数据遵循其实际来源的独立使用条件。
