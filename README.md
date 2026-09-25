# 搭建家风故事采集与分级开放服务基础服务

本项目提供节日公共服务场景的通用后台基础层，负责组织、服务站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域模块可以在这些稳定边界之上增加自己的状态、规则和接口，而不必重复实现身份、站点与审计能力。

## 目录

- `src/festival_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/family_stories/`：家风故事领域模块——不可变版本、叙述对象授权、三阶段复核队列、脱敏视图、访问审计与批量导入；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

## 家风故事模块（family_stories）

在基础层之上实现采集与分级开放：

- **不可变版本**：访谈提纲、结构化人物引用、资料摘要随每次编辑追加新版本（`story_versions`），旧版本与其复核意见永不覆盖；
- **授权管理**：按叙述对象逐条登记开放范围（`private < class < community < public`），收窄请求立即生效，历史授权链保留；公开范围取所有有效授权的安全交集；
- **复核队列**：事实核对（reviewer/admin）→ 敏感信息复核（admin）→ 授权确认（operator/admin）依次推进，提交者回避；审核租约过期后旧审核人不得继续决定，租约可被他人接管；任一步退回即终止该版本，修订另开新版本；
- **脱敏视图**：受众视野（class/community/public）只看到最新已发布版本，实际开放度取「请求视野、当前授权交集、发布时范围」三者最窄；姓名仅在 public 且本人授权 public 时可见；每次访问（含被隐藏的）写入不可变访问审计；
- **审计解释**：`GET /stories/{id}/explain` 向审计员给出授权链、意见链、版本链与访问史，解释材料为何被隐藏、收窄或替代；
- **批量导入**：`POST /batches` 同一事务全有或全无；`source_id` 重复上传识别为重放，内容不同则拒绝。

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
PYTHONPATH=src python3 -m family_stories.acceptance
```

验收命令会在临时 SQLite 数据库中登记组织、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。家风故事验收额外覆盖三阶段复核、发布、授权收窄、访问审计与批量导入重放。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m family_stories.api --database family_stories.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。家风故事接口：`POST /stories`、`POST /stories/{id}/revisions`、`POST /stories/{id}/consents(:narrow)`、`POST /stories/{id}/review-claims|review-decisions|publication`、`GET /stories/{id}/view?view_scope=`、`GET /stories/{id}/explain`、`GET /review-queue`、`GET /published?view_scope=`、`GET /access-audit`、`POST /batches`；基础层接口保持不变（也可继续用 `python3 -m festival_foundation.api` 单独启动）。
