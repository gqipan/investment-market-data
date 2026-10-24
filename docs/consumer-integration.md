# 私人应用的市场数据接入契约

## 数据方向与当前交付

数据仓库发布经过批准的市场数据；私人应用只向数据仓库读取固定版本。私人应用的账本、关注原因、账户、持仓、净值、数据库、报告和凭据不得回传或由数据仓库工作流检出。两边不共享业务数据库，不在网页请求期间同步下载全历史数据。

已实现 `stage-release`、`fetch-release` 和 `shadow-check`：下载指定不可变公开 Release，校验调用方锁定的清单哈希和数据集 ID，安装独立缓存，再只读对照固定版本。尚未部署生产定时下载、自动切换版本或估值导入，也未改动私人应用代码或生产环境。

## 第一阶段：离线研究读取器

已有国内研究 CLI 的输入不仅要求 `bars.csv` 和 `manifest.json`，还要求 `market=A`、人民币、上海时区日线、不复权、明确交易日历和合法数据集身份。源 catalog 使用 `market=CN`，因此消费端显式转换为 `A`。转换后的 `dataset_id` 重新计算，另保留：

- `upstream_release_id`
- `upstream_release_manifest_sha256`
- `upstream_dataset_id`
- `consumer_adapter_version=legacy-cn-reader-v1`

转换仅改变元数据，不改变 OHLCV 数值或源快照。不同适配器版本生成不同消费目录，不能静默覆盖。

正式消费方配置至少锁定 `release_id`、`release_manifest_sha256` 和 `dataset_id`。这些值应来自审阅通过的版本登记，不从刚下载的、不可信清单自行生成后当成信任依据。消费端无需向数据仓库提交自己的缓存位置或研究配置。

已下载完整数据包后：

```bash
PYTHONPATH=src python -m market_data stage-release \
  --release-dir "$DOWNLOAD_DIR" \
  --manifest-sha256 "$RELEASE_MANIFEST_SHA256" \
  --dataset-id "$DATASET_ID" \
  --cache-dir "$MARKET_DATA_CACHE"
```

命令输出可传给国内研究 CLI 的 `--dataset`。缓存布局为：

```text
<cache>/<release_id>/<upstream_dataset_id>/legacy-cn-reader-v1/
  bars.csv
  calendar.csv
  manifest.json
```

消费端会验证包内白名单、哈希、内容身份、来源、交易日、行数和日期，然后原子安装到新目录。同一版本重跑会核对期望的派生身份后复用；缓存自洽但与固定上游内容不符时也拒绝。失败不覆盖旧版本、不修改 `current.json`、不导入数据库。研究报告应同时记录源身份与消费身份。

合成样例默认拒绝，CI 冒烟测试须显式传 `--allow-synthetic`。它不能作为正式研究输入或真实行情发布。

## 第二阶段：下载与生产影子验证

固定版本包路线已选定。未来正式数据获准发布后，在私人应用侧编排后台下载任务：

1. 使用只读凭据读取指定仓库和固定 tag；私有仓库需要认证，公开包可以匿名读取。凭据只放消费端环境，不写进 URL、manifest 或公共仓库。
2. 下载到临时目录，检查大小与完整性，调用 `stage-release`，禁止使用漂移的 Latest 链接作为研究输入。
3. 先通过源数据键对照应用中的 `market/asset_type/symbol/adjust`，不可只用六位代码路由。
4. 先用于独立研究读取；生产技术指标缓存另做格式适配，继续保持与业务数据库物理隔离。
5. 连续五个交易日只读对照现有行情、单位、缺日和指标结果；验证源站断网、半包、过期、撤回、缓存损坏和回退。
6. 只有验收通过后，才在私人应用侧原子切换“有效版本”指针；单个后台任务负责切换，网页请求只读已经生效的缓存。失败沿用上一有效版本并显示数据状态。

`stage-release` 本身不联网；`fetch-release` 提供匿名下载组件，但不刷新凭据、不运行后台任务、不切换指针。私人系统的任务注册、缓存适配与指针管理仍需另外实施，不能把命令描述成已部署的网页或自动同步服务。

指定版本下载命令（当前没有正式真实行情 Release，下例需使用审阅通过的实际登记值）：

```bash
PYTHONPATH=src python -m market_data fetch-release \
  --repository gqipan/investment-market-data \
  --release-id "$RELEASE_ID" \
  --manifest-sha256 "$RELEASE_MANIFEST_SHA256" \
  --dataset-id "$DATASET_ID" --cache-dir "$MARKET_DATA_CACHE"
```

[GitHub Release API](https://docs.github.com/en/rest/releases/releases#get-a-release-by-tag-name) 提供固定 tag 和不可变标记。下载器只接受正式、不可变 Release；拒绝草稿、预发布、跨仓库资产、额外文件、错误尺寸和哈希。固定清单须允许公开消费；合成数据只能显式用于冒烟。HTTPS 和重定向仅允许 GitHub 资产域名，连接超时 20 秒，不发送任何认证头。元数据各限 1 MiB，压缩包限 50 MiB。下载失败不创建新消费版本，不回退到 Latest、其他来源或合成 artifact。私人 Release 认证暂不支持。

影子对照使用两个经过审阅并锁定消费清单哈希的独立缓存：

```bash
PYTHONPATH=src python -m market_data shadow-check \
  --candidate "$CANDIDATE_CACHE" --baseline "$BASELINE_CACHE" \
  --candidate-manifest-sha256 "$CANDIDATE_MANIFEST_SHA256" \
  --baseline-manifest-sha256 "$BASELINE_MANIFEST_SHA256" \
  --expected-session "$LAST_OPEN_SESSION" \
  --minimum-sessions 900 --relative-tolerance 0
```

最近交易日由独立交易日历确定，不能直接使用自然日。报告 JSON 写标准输出，比较通过退出 0，不通过退出 1，输入损坏退出 2。比较覆盖身份、单位、日历哈希、共同日期、缺日、末日新鲜度和 OHLCV 差异。输出不含本地路径。相同数据集自我比较会标为 `same_dataset=true`，只证明读取路径，不能算独立影子验收。容差和历史要求在运行前冻结，不能为通过检查而临时降低。

默认 900 个交易日为 850 日均线保留缓冲；当前六日样本会报告历史不足，不能覆盖生产历史缓存。降低行数只可用于开发冒烟，不能算生产验收。每份报告恒定 `promotion_allowed=false`；五个连续交易日的独立对照和正式切换仍由私人应用完成。

私人系统现有 A 股刷新时刻为 19:30，本仓库校验时刻为 20:17，不能直接令前者等待后者的当天数据。未来真实发布启用后，建议消费端在 21:00 后按固定版本登记检查发布完成和实际交易日，不依赖 GitHub 准点运行；失败沿用旧有效缓存。研究 CLI 先接入；生产 MA 缓存需独立适配，指数不复权与 ETF 前复权缓存保持分开。

GitHub Actions 的 synthetic artifact 只用于验证传输、打包和消费契约，不是正式 Release，也不能用当前合成数据替换线上行情。当前价格与日历来源的云端保存／发布开关仍未批准。

## 第三阶段：估值与应用指标

估值必须另建观测数据集，明确 PE 的 TTM/静态、整体法/中位数、PB 方法和股息率单位。保留发布方和来源日期，禁止从行情推导原始 PE/PB 或把缺失补零。

应用中的估值历史、分位数和展示缓存需单独设计导入契约。原始观测与应用派生指标分开保存；不得为接入新市场数据重算或写入私人持仓、交易、现金流、每日盈亏及资产净值。本轮没有启用这一阶段。

## GitHub 验收

`check` 工作流在 Ubuntu / Python 3.11 上运行单元测试、目录与隐私检查，构建合成候选、验算数据包、执行消费格式转换，并确认私有和公开发布门禁都拒绝合成数据。仅上传明确路径下的合成候选和通用目录报告；不上传 `data/`、整个工作区或私人项目。

验收记录必须包括真实 commit、工作流 run URL、artifact、清单哈希和数据集 ID。以后接入真实数据时，价格来源和独立日历来源的权限分别核查，不能用一方许可代替另一方。
