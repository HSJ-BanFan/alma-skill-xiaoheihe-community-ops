# Issue 协作与交接约定

在开始一个切片前，核对 [分期路线图](../ROADMAP.md)、[领域索引](domain.md) 与 [分诊标签](triage-labels.md)。以当前任务授权、Issue 规格和源码为范围依据。

## 定义一个可验收切片

1. 写明用户目标、授权来源和对应 Issue。
2. 记录刷新后的 `origin/main` SHA、当前 HEAD 与独立 Worktree 分支。
3. 列出允许文件、禁止重叠范围、前置依赖与停止条件。
4. 为每项验收条件指定真实命令、预期行为和证据位置。
5. 区分已观察结果、推断与 `not_tested`，列出保留缺口。

遇到规格与源码不符时，保存实际结果并回报负责协调者。测试或历史摘要不允许成为扩大范围的理由。

## 阶段与任务隔离

Phase 1 对应 [Issue #17](https://github.com/HSJ-BanFan/alma-skill-xiaoheihe-community-ops/issues/17)，联动 Web 发布问题 [Issue #12](https://github.com/HSJ-BanFan/alma-skill-xiaoheihe-community-ops/issues/12)。Phase 2 对应 [Issue #18](https://github.com/HSJ-BanFan/alma-skill-xiaoheihe-community-ops/issues/18)，Phase 3 对应 [Issue #19](https://github.com/HSJ-BanFan/alma-skill-xiaoheihe-community-ops/issues/19)。具体推进顺序以路线图和单独授权为准。

SA-03 只新增指定的八个工作流文档。它与 PR #21、SA-01 及其打包分支隔离，不读取或复用其评审中代码，不承接其 CI 修复。`tests/verify_portable.py` 的历史摘要约束保持原样，P1-2、P1-3 与 Phase 2、Phase 3 保持门禁。

## 本地待审交接

1. 只提交授权路径，提交信息附任务要求的共同作者署名。
2. 在最终本地 commit 后执行 `.agents/skills/verify-xhh-tools/SKILL.md` 的验证步骤。
3. 保存每条命令的参数、环境、原始 stdout、stderr、退出码与负向控制结果。
4. 核对相对基线的文件清单、`git diff --check` 与 `git status --porcelain=v1`。
5. 回传完整 commit SHA、分支、Worktree、基线、文件列表与仓外报告路径。

检查通过形成本地 build-ready 待审结果。独立复审、远端推送、PR 创建、Ready、Merge、tag、Release 与部署分别需要当前任务范围内的授权。SA-03 只允许本地提交，不执行这些远端动作。

## 失败与未覆盖项

任何测试失败都保存原始日志并回报阻塞。保持现有测试、依赖与业务代码原样，不冻结计数或摘要，也不删校验绕过门禁。

交接报告必须明确离线证据的边界。真实登录、发帖与评论、公开可见性、创作者计划审核、App 签名器在线调用、Issue #12 与 PR #21 的打包及 Windows 修复为 `not_tested`。

根 `AGENTS.md` 因常设禁令仍缺失，仓库代理入口尚未闭环。SA-03 的八个文档完成后仍保留该缺口。
