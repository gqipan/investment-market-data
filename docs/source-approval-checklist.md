# 数据来源审批清单

每个拟发布数据集必须保留可审计的来源依据。审批人需要确认下列事项，而不是只确认“页面能打开”。

## 来源身份

- 数据发布者与数据权利人是同一方，还是存在再许可关系。
- 数据集名称、代码、口径、价格类型、复权方式和成交量单位。
- 官方使用条款、许可证或书面授权链接。
- 核查日期与核查版本。

## 允许用途

- 是否允许自动采集，包括在 GitHub-hosted runner 上采集。
- 是否允许长期保存规范化副本。
- 是否允许在私有 GitHub 仓库中托管。
- 是否允许在公开 GitHub 仓库中托管。
- 是否允许向匿名用户下载和再分发。
- 是否允许商业使用；本项目即使不收费，也不能忽略该限制。

## 技术可信度

- 历史起点与指数发布日期的区别。
- 停牌、无行情、源站延迟和节假日如何表达。
- 后续修订是否带可追溯通知。
- 主源和备选源的口径能否重叠校验。

## 私有发布结论

只有下列字段同时满足，才允许把来源状态改为 `approved_private`：

```json
{
  "status": "approved_private",
  "private_release_allowed": true,
  "cloud_collection_allowed": true,
  "cloud_storage_allowed": true
}
```

## 公开发布结论

只有下列公开字段同时满足时，才允许把来源状态改为 `approved_public`：

```json
{
  "status": "approved_public",
  "public_release_allowed": true,
  "cloud_storage_allowed": true,
  "cloud_collection_allowed": true,
  "redistribution_allowed": true
}
```

私有仓库可以接受只覆盖私人研究的授权，但不能把同一来源自动放行到公开 Release。任何一项没有明确依据都保持 `pending_authorization`。口头允许、普通网页版权声明、软件开源许可证或“其他 GitHub 项目也在发”均不充分。

## 采集与发布分别登记

`local_collection_allowed` 只控制本地研究采集。`tencent_raw` 和 `akshare_eastmoney` 已接入本地价格采集，但 `status` 仍为 `pending_authorization`，不代表已经批准云端访问或分发。中证指数编制方身份不能替代实际腾讯／东方财富上游登记。

公开发布还需 `cloud_storage_allowed: true`；发布时重新核查当前开关，旧候选不保留已撤销的授权。
