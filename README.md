# Investment Market Data

可审计的市场数据采集、校验与打包工具。通用代码可以公开托管，真实数据是否发布按实际来源单独判断；指数编制方不等于必须直连的采集接口。公开仓库仅包含通用代码、配置和合成样例，真实快照只保存在本机。

## 当前可用能力

- 11 个指数已登记独立身份、价格与估值数据集。
- 价格默认使用腾讯原始日线 `tencent_raw`，无需中证官网；930955 暂无可用腾讯映射；可显式选择 `akshare_eastmoney`，经 AkShare 调用东方财富不复权指数日线。
- 本地 `collect` 命令生成独立快照，包含行情、独立交易日历、实际来源、单位、日期、SHA-256 和内容身份。
- 支持合成样例或真实本地快照生成候选包；验算检查文件白名单、压缩包、隐私、哈希、行数和交易日覆盖。
- `release-check` 额外检查当前来源权限、真实 Git commit、代码一致性、干净工作树和仓库文件白名单。合成数据始终不能正式发布。
- 每日 Actions 只做测试、隐私和 catalog 检查，不进行未获准的云端取数或上传真实数据。

2026-10-24 本地验收：沪深300经腾讯原始接口取得 2026-10-16 至 2026-10-23 的 6 个交易日，深交所整月独立日历、打包和离线验算通过。11 个指数逐项联网验收中，10 个各通过该区间 6 个交易日的检查；930955 腾讯原始序列缺失，已移除其无效映射，默认采集在联网前拒绝。AkShare/东方财富在本次网络环境连接失败，适配器仅通过离线接口测试。估值仍是目录占位，没有估值采集器。远程正式 Release 和生产应用接入尚未启用。

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

本地研究采集与云端采集、保存、公开再分发分别登记。当前两个价格来源启用了本地研究取数，发布范围仍为待核查；运行在 GitHub Actions 中时，未获云端采集许可会直接失败。

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

合成测试样例：

```bash
PYTHONPATH=src python -m market_data build-candidate \
  --dataset csi300-demo --as-of 2026-10-22 --revision 1 --output dist
PYTHONPATH=src python -m market_data verify-release \
  --release-dir dist/data-2026-10-22-fixture-r1
```

候选清单使用 schema 1.2，旧版合成候选需重新构建。候选目录仅包含 `release-manifest.json`、`quality-report.json`、`SHA256SUMS` 和一个压缩包；包内仅允许同一目录中的 `bars.csv`、`calendar.csv`、`manifest.json`、`README.txt`。保留来源精度，不强制将价格四舍五入到两位。候选已通过验算不等于允许正式发布。

## 私人应用接入

已提供 `stage-release`：调用方锁定清单哈希和数据集 ID 后，生成不可变的消费缓存，并将国内研究 CLI 所需的 `CN` 转为 `A`，保留上游身份。它不会修改有效版本指针或业务数据库。后台下载、生产缓存适配、五个交易日影子对照和估值接入的边界见[消费端接入契约](docs/consumer-integration.md)。

## 发布边界

```bash
PYTHONPATH=src python -m market_data release-check \
  --root . --release-dir dist/data-2026-10-23-tencent_raw-r1 --scope public
```

当前真实来源未获公开发布批准，因此上例应拒绝。工作树有修改时，自动记录的版本为 `local-uncommitted`，也不能正式发布。价格与独立日历来源分别核查。发布门禁会重新读取当前来源开关，撤销权限后旧候选同样失效；不能只相信候选里的旧批准标记。

代码公开、数据公开、仓库当前可见性分别管理。本项目不会因为指数属于中证，就强制将整个代码仓库改成私有；使用 AkShare 也不自动给数据授予再分发权。来源身份、使用条件和发布范围见 [数据政策](DATA_POLICY.md) 与 [来源清单](catalog/sources.json)。

- [指数与来源说明](docs/index-catalog.md)
- [来源审批清单](docs/source-approval-checklist.md)
- [维护与验收记录](docs/operations.md)

代码使用 MIT License；数据遵循其实际来源的独立使用条件。
