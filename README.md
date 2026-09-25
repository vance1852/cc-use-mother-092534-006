# 家风故事采集与分级开放服务

本项目提供节日公共服务场景的通用后台基础层，负责组织、服务站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。家风故事模块在这些稳定边界之上实现采集、不可变版本、分级同意、脱敏视图与三步复核队列。

## 目录

- `src/festival_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/festival_foundation/stories_*.py`：家风故事采集与分级开放（版本、同意、脱敏、复核、批量导入）；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m festival_foundation.acceptance
PYTHONPATH=src python3 -m festival_foundation.stories_acceptance
```

验收命令会在临时 SQLite 数据库中登记组织、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。家风故事验收额外覆盖：退回生成新修订且旧意见保留、多位叙述对象授权取安全交集、脱敏视图按级别生效、收窄后即时关闭、批量导入原子性与来源重放。

## 家风故事开放规则

- **不可变版本**：每次首次提交、提交者更正、复核退回都写入新修订（`story_revisions`），内容带父版本哈希；复核意见挂在具体版本上，永不覆盖。
- **分级同意**：`private`（班级分享）< `community`（社区展览）< `archive`（公开资料库）。授权声明按叙述对象逐条累积，撤回/收窄只追加新声明；涉及多位叙述对象时，公开范围取所有最新有效授权的最小级别，任一对象无授权或撤回即整体不可见。补充人物关系产生新修订，新对象无授权会阻止再次发布。
- **脱敏视图**：敏感信息复核人对摘要或结构化人物引用打标（mask/hide/replace），标注声明触发的最低级别；班级视图可保留原文，社区及以上自动替代或隐藏。提交者可请求更正或缩小未来可见范围，历史授权声明与访问审计均保留。
- **三步复核**：`fact_check` → `sensitive_check` → `consent_confirm`，必须由三位不同责任人完成；认领获取带过期时间的租约，租约过期后旧审核人不得再决定，任一步退回即生成新修订并重置队列。
- **批量导入**：整批校验后统一提交，任一条目失败全部回滚；同一 `source_id` 重复上传识别为来源重放，内容被篡改则拒绝。
- **审计解释**：`GET /story-visibility-explanation` 供审计人员查看每个版本的意见、脱敏标注、授权时间线和各级别可见性，解释某段材料为何被隐藏、收窄或替代；被拒绝的访问同样写入哈希链。

### 家风故事接口

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /stories` | operator/admin | 提交故事（访谈提纲、人物引用、摘要） |
| `POST /story-corrections` | operator/admin | 提交者更正，生成新修订 |
| `POST /story-consents` | operator/admin | 登记某叙述对象的分级授权/撤回 |
| `POST /story-visibility-narrowings` | 原提交者/admin | 缩小未来可见范围 |
| `POST /redaction-marks` | reviewer/admin | 脱敏标注（限敏感复核未结束时） |
| `POST /review-claims` / `POST /review-decisions` | reviewer/admin | 认领租约 / 通过或退回 |
| `POST /story-publications` | operator/admin | 三步通过且授权齐全后发布 |
| `GET /published-story?story_id=&scope=` | 本站授权用户 | 按级别返回脱敏后的可发布内容 |
| `GET /stories?site_id=` | 本站用户 | 故事与各复核步骤状态 |
| `GET /story-visibility-explanation?story_id=` | auditor/admin | 可见性审计解释 |
| `POST /story-imports` | operator/admin | 批量导入（全有或全无、来源重放） |

## HTTP 服务

```bash
PYTHONPATH=src python3 -m festival_foundation.api --database festival_foundation.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。
