# 隐私防护与发布检查

本仓库公开。允许通用采集代码、公开数据契约、已批准的指数数据和合成示例；禁止私人身份、消费端实现、账户账本和凭据。Agent 约束见根目录 [AGENTS.md](../AGENTS.md)。

## 本地提交前

```bash
PYTHONPATH=src python -m market_data privacy-check --path . --repository
PYTHONPATH=src python -m market_data privacy-check --path . --repository --staged
```

第一条检查 Git 已跟踪文件和未忽略的候选文件；第二条读取 Git index 中真正准备提交的版本。忽略的本地数据缓存不属于代码提交检查范围；强制跟踪仍受到文件白名单和内容检查。目录扫描不带 `--repository` 时检查指定目录的文件；最终附件另有门禁，不能用忽略规则跳过。

暂存检查入口已提供，但没有自动修改本机 Git hooks。每次提交前必须运行。新提交使用 `--commit-range <base-sha>..<head-sha>`，检查中间版本、作者/提交者身份和说明，不能只检查最终差异。至多检查 1000 个提交，超出时失败并要求分段检查。

Git 作者与提交者应使用允许公开的姓名和 GitHub noreply 邮箱。CI 对新提交拒绝非 noreply 邮箱；测试用 `.invalid` 域和 GitHub 系统 noreply 身份允许。CI 不替用户决定身份。

## 私人词表与发布正文

私人项目名、内部文件名和部署关系不能靠通用正则穷举。按 AGENTS.md 人工检查；必要时在仓库外保存逐行词表，用环境变量 `MARKET_DATA_PRIVATE_TERMS_FILE` 或 `--terms-file` 指定。空行和 `#` 注释忽略；每项至少三个字符，最多 256 项。不自动读取私人目录，不把词表存入公开测试、CI、日志或附件。指定文件不可读或不合格时失败。

在发布 PR 标题正文、评论、Release note 前，先把拟发布文字保存为仓库外文件，再检查：

```bash
PYTHONPATH=src python -m market_data privacy-check --path . --staged --text-file /tmp/publication.txt
```

扫描只报告受保护的位置、行号和类别，不打印命中原文。通过后仍需人工确认没有私人实现。CI 在推送后运行，不能追回已经公开的提交或正文；本地和发布前复核不可省略。

## 自动防护

- `check` 在测试前扫描实际 Git index；push/PR 还检查本次引入的提交。手动与定时检查不声称覆盖了全部历史。
- 所有工作流在 artifact 上传前扫描最终目录。每日发布先用 `prepare-publication` 准备固定五个附件，通过后才调用 GitHub 创建 Release。
- 附件准备拒绝额外文件、符号链接和私人内容，排除根请求缓存。规范化 tar/gzip 去除 uid/gid、用户名、组名和本机时间；随后扫描压缩包正文、元数据、独立报告和校验文件。失败不生成最终上传目录。
- 扫描覆盖常见密钥、认证头、URL 凭据、本机路径、联系方式字段和可疑文件名。压缩包逐项读取，不解压到磁盘；拒绝不安全路径、链接、嵌套归档和超过上限的内容。

来源权限和质量门禁独立执行；隐私通过不等于数据完整或允许启用生产。修改工作流或扫描器仍需代码审查。仓库管理员的分支保护配置不会由本地改动自动生效。

合并前应核对最终提交的作者和提交者身份；GitHub 的 rebase 合并可能重新生成提交者身份。使用经检查的公开身份生成提交，验证通过后采用不会改变该身份的合并方式，并复核主分支检查结果。

## 已有泄露的处置

1. 停止受影响出口，在仓库外保存最小位置清单，不复制敏感原文。
2. 如涉及真实凭据，先撤销或轮换，再处理文件和历史。
3. 枚举旧提交、分支、标签、PR 差异/评论、Release 源码归档与附件、Actions 日志/artifact/cache 及 fork。
4. 历史重写、移动标签、删除远程内容前，列明固定版本及下游校验关系并取得具体授权。
5. 分开报告当前文件、Git 可达历史、平台残留及不可收回副本的状态；不承诺召回他人的克隆。

参见 [GitHub 敏感数据清除指南](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)。平台是否协助移除缓存由其政策和审查决定。
