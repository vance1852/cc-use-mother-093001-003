# 编排盲审与利益回避协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力，负责项目机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

在此基础上内置**盲审与利益回避后台**：登记评委任职、合作、亲属、师生、同属协会或同机构关系以及作品团队/机构关系，批次创建时冻结关系快照与规则版本，生成确定性、可解释的匿名分配方案；评委只能看到自己的匿名任务，回避、缺席、评分作废按候补队列顺序补位，达到法定人数且专业覆盖完整后才能封存，封存后的评审事实永久保留。纪检人员可复核每次分配的关系快照、规则版本、回避原因与替补顺序。

## 目录

- src/creative_program_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - review_models.py / review_rules.py / review_service.py：盲审数据对象、规则快照与分配引擎、后台用例；
- tests/：基础规则、事务边界、接口路由、盲审分配/回避/封存/并发和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m creative_program_foundation.acceptance

验收命令会在临时 SQLite 数据库中登记项目机构、操作者、业务节点和参考资料，核对幂等回执与审计链，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

## 盲审与利益回避接口

角色沿用 admin / operator（评审秘书）/ reviewer / auditor（纪检）。所有写接口使用 `X-Actor-Id` 与 `request_id` 实现幂等。

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | /review/disciplines | 秘书 | 登记专业领域 |
| POST | /review/reviewers | 秘书 | 登记评委档案与专业（须先存在 reviewer 角色账号） |
| POST | /review/works | 秘书 | 登记作品（匿名编号、赛道、必修专业、法定评委数、材料） |
| POST | /review/team-members | 秘书 | 登记作者/联合作者/机构关系（对评委不可见） |
| POST | /review/conflicts | 秘书或评委本人 | 声明任职/合作/亲属/师生/协会关系，以及同机构自动回避 |
| POST | /review/rule-versions | 秘书 | 创建规则版本（独立人数下限、专业覆盖、赛道负载上限、候补池、关系类型） |
| POST | /review/batches | 秘书 | 冻结关系快照并生成可解释方案（草稿；不可行时列明原因） |
| POST | /review/batches/publish | 秘书 | 复核可行后发布批次 |
| POST | /review/batches/close | 秘书 | 全部作品封存后关闭批次 |
| POST | /review/tasks/claim | 评委本人 | 领取任务（条件更新保证并发只有一次成功） |
| POST | /review/tasks/recuse | 评委本人 | 临时回避，自动按顺位提升候补 |
| POST | /review/tasks/absent | 秘书 | 记录缺席并自动补位 |
| POST | /review/scores | 评委本人 | 提交评分（唯一约束防止重复） |
| POST | /review/scores/void | 秘书 | 作废评分但保留事实，触发候补补位 |
| GET  | /review/my-tasks | 评委 | 只看自己的匿名任务；材料在领取后、回避后立即关闭 |
| POST | /review/seal | 秘书 | 有效评分达到法定人数且覆盖必修专业后封存 |
| GET  | /review/batches | 秘书/纪检 | 批次列表 |
| GET  | /review/batches/{id}/audit | 纪检 | 关系快照及哈希、规则版本及哈希、回避原因、任务轨迹、替补顺序、评分（含作废） |

关键约束：秘书与管理员**不能**提交或修改评分；封存后任务和评分不得再调整；所有状态落 SQLite，进程重启不会打乱已接受任务或候补队列。
