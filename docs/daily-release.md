# 每日累计数据发布

工作流定义： [daily-release.yml](../.github/workflows/daily-release.yml)。单数据集 `release-manifest.json` 包与此累计格式分别使用，详见[单数据集快照](snapshot-release.md)。

`daily index release` 每天北京时间 21:40、23:40 和次日 09:00 检查一次。定时 09:00 运行只检查截至昨天的已闭市交易日，避免盘中把当天误当成已完成会话。每次运行都会读取深交所独立交易日历，找出上一版 Release 之后尚未发布的全部已闭市交易日；没有待发布会话时正常结束，不创建空版本。日历源不可用时工作流失败，而不是把未知状态当成休市。

这个安排用来处理上游延迟：如果周五数据在周五 21:40 或 23:40 尚未齐备，工作失败且不发布；周六 09:00 会继续尝试。周六补齐时仍发布 `data-<周五日期>-r1`，不会给周六生成一个空 Release。若一次积压多个交易日，工作流把连续缺失区间合并为一个补缺批次，Release 以其中最新交易日命名。

## 第一次全量基线

只有首次建库才手动触发并设置 `bootstrap=true`；现有正式基线无需重建。手动 `as_of` 作为请求上界，不能晚于当天；运行人需选择已经闭市的日期，盘中手动指定当天并没有额外收盘时刻保护。基线使用 `2005-01-01` 作为请求起点，可以包含在指数正式发布前没有观测的明确前缀。生成目录按以下结构保存：

```text
baseline/
cumulative-manifest.json
```

基线中的每份内容寻址数据集必须先通过 manifest SHA-256、`dataset_id`、正文哈希、交易日历和数据类型复验。仓位、账户、现金流、私人报告及服务器配置不属于输入范围。

## 后续每日增量

普通定时运行或手动设置 `bootstrap=false` 时，工作流请求上一版之后的连续缺失交易日，并执行以下步骤：

1. 在最近 100 个 Release 中，按发布时间选择最近一个正式、非预发布的 `data-YYYY-MM-DD-rN` 累计版本，并核对 tag 日期与累计清单中的 `as_of`。
2. 查询交易日历中从上一版次日至当前允许上界之间的全部已闭市交易日；没有缺失会话时直接结束。
3. 下载上一版累计压缩包和 `SHA256SUMS`，先核对压缩包哈希，再用安全解压器忽略 `._*`、`__MACOSX/` 等 macOS 元数据，复验 `cumulative-manifest.json` 的全部文件清单及其 `as_of` 与上一版 tag 的一致性。
4. 采集连续缺失区间，逐个复验批次内价格、收盘价和估值清单。上一版已经存在的每个指数，其本次价格或收盘序列都必须到达区间末日；任一已建立指数暂时不可用、仍是旧日期或序列为空时立即失败，后续定时检查重试同一区间，不推进 Release。
5. 将补缺结果作为不可变批次追加到 `deltas/<末日日期>/`，不覆盖基线或旧批次。
6. 重新生成累计清单，并把基线及全部批次按同一来源材料化为当前研究快照；材料化结果必须通过全部 manifest、正文、数据身份和隐私复验，才继续打包。
7. 生成完整累计包、补缺增量包和 `SHA256SUMS`。
8. 先上传为草稿，全部附件成功后再正式发布。失败不修改上一正式版；采集或验算失败在下次运行重试同一区间，上传失败若留下同日期草稿则需先核查处理，详见[维护与恢复](operations.md)。

基线要求至少一个价格或收盘序列到达末日；后续版本额外要求上一版已建立的所有指数都到达末日，没有固定“至少 9 条”门槛。估值可能仍为月度或快照，覆盖报告整体为 partial 不会单独阻止归档；工作流显式使用 `backfill --allow-partial`，随后独立执行质量与末日门禁。

`cumulative-manifest.json` 保存上一版 tag、上一版累计包 SHA-256、批次顺序以及当前所有文件的 SHA-256。相同日期不能追加两次，不能原地覆盖。当前工作流固定生成 `data-<日期>-r1`，没有 revision 输入；历史修订需要另行准备与验算新版本，不能直接重跑当前定时流程实现 r2。

## Release 文件

- `index-data-cumulative-<日期>.tar.gz`：完整历史基线和截至当日的全部增量。
- `index-data-delta-<日期>.tar.gz`：工作流输出的本次连续缺失区间采集结果，便于核对新增变化。首个已发布基线只有累计包，没有独立 delta 附件。
- `SHA256SUMS`：该版本实际归档压缩包的 SHA-256。
- `coverage-report.json` / `.md`：当天来源覆盖、缺口和不支持映射。

下载方应锁定 tag 与 `SHA256SUMS`，不使用漂移的 Latest。Git clone 不包含 Release 附件；需要长期保存时另行归档累计包。

## 下载与研究读取

首次安装直接下载完整累计包；delta 包用于检查新增批次，不是独立全历史缓存。下面固定到已发布基线，哈希来自 2026-10-25 核对的 GitHub 附件 digest。后续版本需要独立登记对应 tag 和压缩包哈希，不能从未校验的下载内容自行生成信任锚。

需要 GitHub CLI，示例从仓库根目录运行。下载、解包和材料化分别使用新目录；重跑时更换目录名。`python` 可替换为本机的 `python3`。

```bash
gh release download data-2026-10-23-r1 \
  --repo gqipan/investment-market-data \
  --pattern index-data-cumulative-2026-10-23.tar.gz \
  --pattern SHA256SUMS \
  --dir dist/download-baseline

python - <<'PYHASH'
import hashlib
from pathlib import Path
archive = Path("dist/download-baseline/index-data-cumulative-2026-10-23.tar.gz")
expected = "d2fa0f9e81258e4eccefb8f1d7999286319944730bfe4b8270c1768dbf54cc09"
actual = hashlib.sha256(archive.read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit("cumulative archive SHA-256 mismatch")
print("cumulative archive SHA-256 verified")
PYHASH

PYTHONPATH=src python -m market_data extract-cumulative \
  --archive dist/download-baseline/index-data-cumulative-2026-10-23.tar.gz \
  --output data/local/baseline-extracted
PYTHONPATH=src python -m market_data materialize-cumulative \
  --root data/local/baseline-extracted \
  --output data/local/baseline-research
PYTHONPATH=src python -m market_data verify-registry-cache \
  --root data/local/baseline-research \
  --registry data/local/baseline-research/registry.json
```

`extract-cumulative` 只负责安全解包，不校验外层下载哈希；哈希必须先核对。它拒绝路径穿越、链接、特殊文件、重复路径和过量成员；`materialize-cumulative` 随后复验累计清单和各数据集正文，同一来源、指数和数据类型按顺序合并，不跨来源拼接。重叠日期值冲突时拒绝。

生成结果为 `registry.json` 加若干以 manifest SHA-256 命名的目录。登记包含 `symbol`、`source`、`data_class`、`cache_name`、`manifest_sha256`、`dataset_id`；顶层保留 `expected_session`，并固定 `promotion_allowed=false`、`usage=local_research_only`。读取方继续核对类型、频率、来源和所需历史，不能仅凭注册成功启用生产。

累计包不含单数据集的 `release-manifest.json`；`fetch-release`、`fetch-snapshot-release`、`verify-release` 与 `release-check` 不接受此格式。每日工作流执行的是闭市会话计算、采集权限检查、逐批正文验算、已建立序列末日检查、累计清单与材料化复验、隐私扫描及草稿发布流程，不等同于单数据集候选的 Git 来源门禁。

## 已发布基线

截至 2026-10-25 核对，正式不可变版本为 [data-2026-10-23-r1](https://github.com/gqipan/investment-market-data/releases/tag/data-2026-10-23-r1)，数据截至 2026-10-23。Release 说明记录 11 份 OHLCV、6 份独立收盘序列、22 份估值观测，包含同指数不同来源，不代表 39 个指数或完整日频估值。当前验收与失败恢复见[维护与恢复](operations.md)。
