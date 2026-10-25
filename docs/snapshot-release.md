# 单数据集采集、打包与发布门禁

本文针对单数据集候选与固定版本消费包。每日公开累计包使用另一套格式，见[每日累计发布](daily-release.md)。以下固定日期为可复现用法示例；输出目录必须是新目录，已有目录应改用新的运行名。历史快照路径取决于实际回补结果，先检查 manifest 的 data_class 和末日。

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

Sina 原始指数接口（完整 OHLCV，最多 2,000 个交易日）：

```bash
PYTHONPATH=src python -m market_data collect \
  --dataset csi300-normalized-v1 --provider sina_raw \
  --start 2018-07-24 --end 2026-10-23 \
  --output data/local/csi300-sina-20261023
```

显式选择来源，不自动回退或拼接。腾讯每个请求最多尝试三次，每次超时 15 秒；AkShare 在独立进程内执行，整体超时 120 秒。AkShare 是接口库，清单仍记录实际东方财富端点和库版本。不同来源生成不同 `dataset_key`，不能当作同一历史序列。

本地研究采集、私人云保存和公开再分发分别登记。当前使用来源在 catalog 中已登记所有者的公开发布批准；实际开关以 [sources.json](../catalog/sources.json) 为准。真实正文不提交 Git，获准数据通过 Release 分发。候选验算与正式发布门禁分别执行，见下文。

## 打包与验算

真实本地快照：

```bash
PYTHONPATH=src python -m market_data build-candidate \
  --dataset csi300-normalized-v1 \
  --snapshot data/local/csi300-tencent-20261023 \
  --as-of 2026-10-23 --revision 1 --output dist/manual-ohlcv
PYTHONPATH=src python -m market_data verify-release \
  --release-dir dist/manual-ohlcv/data-2026-10-23-tencent_raw-r1
```

独立收盘价或估值观测：

```bash
PYTHONPATH=src python -m market_data build-snapshot-release \
  --dataset csi300-normalized-v1 \
  --snapshot data/local/history-run/000300/price \
  --as-of 2026-10-23 --revision 1 --output dist/manual-close
PYTHONPATH=src python -m market_data verify-release \
  --release-dir dist/manual-close/data-2026-10-23-tencent_raw-r1

PYTHONPATH=src python -m market_data build-snapshot-release \
  --dataset csi300-valuation-v1 \
  --snapshot data/local/history-run/000300/akshare_legulegu \
  --as-of 2026-10-23 --revision 1 --output dist/manual-valuation
PYTHONPATH=src python -m market_data verify-release \
  --release-dir dist/manual-valuation/data-2026-10-23-akshare_legulegu-r1
```

合成测试样例：

```bash
PYTHONPATH=src python -m market_data build-candidate \
  --dataset csi300-demo --as-of 2026-10-22 --revision 1 --output dist/manual-demo
PYTHONPATH=src python -m market_data verify-release \
  --release-dir dist/manual-demo/data-2026-10-22-fixture-r1
```

完整 OHLCV 候选使用 schema 1.2；独立收盘价与估值使用 schema 1.3。候选目录仅包含 `release-manifest.json`、`quality-report.json`、`SHA256SUMS` 和一个压缩包。OHLCV 包内为 `bars.csv`、`calendar.csv`、`manifest.json`、`README.txt`；收盘价包内为 `close.csv`、`raw-bars.json`、`manifest.json`、`README.txt`；估值包内为 `observations.csv`、`raw.json`、`manifest.json`、`README.txt`。保留来源精度，不强制将价格四舍五入到两位。候选已通过验算不等于允许正式发布。

## 正式发布门禁

```bash
PYTHONPATH=src python -m market_data release-check \
  --root . \
  --release-dir dist/manual-ohlcv/data-2026-10-23-tencent_raw-r1 \
  --scope public
```

`release-check` 识别 schema 1.2 / 1.3，重新核查当前来源开关、数据身份、独立日历（适用时）、隐私、Git commit、代码一致性与干净工作树。当前使用的真实来源已登记所有者的公开批准；候选仍须满足所有门禁才通过。合成样例始终拒绝正式发布，未提交的工作树也不能正式发布。

`--scope private` 使用私人发布权限，不代表公开授权。私人云端字段存在时优先读取 `private_cloud_collection_allowed` / `private_cloud_storage_allowed`，否则回退到公共 `cloud_*` 字段。来源定义见[数据政策](../DATA_POLICY.md)和[审批清单](source-approval-checklist.md)。

本命令只检查候选，不上传附件。每日累计工作流不会调用单数据集的 `release-check`；其实际检查链见[每日累计发布](daily-release.md)。候选通过不能替代每日工作流验收，累计包也不能传给 `verify-release` 或 `fetch-release`。
