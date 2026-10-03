# 编排盲审与利益回避协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力，负责项目机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。并在其上实现了一套**盲审与利益回避后台**：接收评委关系声明与作品团队/机构关系，在不向评委暴露作者身份的前提下生成可解释的分配方案，支持回避、缺席、评分作废、候补递补、例外处理、法定人数封存与纪检复核。

## 目录

- src/creative_program_foundation/：领域模型、SQLite 存储、权限服务、审计链、盲审服务、HTTP 路由和离线验收；
- tests/：基础规则、事务边界、接口路由、盲审规则和端到端验收测试。

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
    PYTHONPATH=src python3 -m creative_program_foundation.acceptance_review

验收命令在临时 SQLite 数据库中跑通完整业务链，输出一行 status 为 ok 的 JSON 并以退出码 0 结束。盲审验收覆盖：顾问/任职/合作/亲属/师生/同属协会六类回避、专业覆盖例外、并发领取去重、发布后重启、评分作废保留、候补递补、紧急替补冲突红线、法定人数封存与纪检快照复核。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者并要求 request_id 幂等键，服务重启后 SQLite 中的业务状态、任务队列和审计历史继续保留。

## 角色

- operator（评审秘书，admin 同权）：登记规则版本、赛道、评委档案、作品、关系声明，发布批次、登记例外、缺席、紧急替补和封存；**不能提交或修改分数**。
- reviewer（评委）：只能查看和领取自己的匿名任务、查看匿名材料、提交评分或自行回避。
- auditor（纪检人员）：只读复核关系快照、规则版本、回避原因、替补顺序、任务事件链与作废但保留的分数。

## 盲审 API

写入均为 POST，JSON 体需含 `request_id`；读为 GET。

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | /review/rule-versions | 秘书 | 登记规则版本（独立评委数、法定人数 quorum、赛道负载上限、候补数），相同内容哈希去重 |
| POST | /review/tracks | 秘书 | 登记赛道 |
| POST | /review/reviewers | 秘书 | 评委档案：专长、可评赛道（须为 reviewer 操作者） |
| POST | /review/works | 秘书 | 登记作品匿名编号、机构、团队 person_key/协会、必需专业、材料地址 |
| POST | /review/relations | 秘书/本人 | 关系声明：advisory/employment/collaboration/kinship/teacher_student/association |
| POST | /review/batches | 秘书 | 创建草稿批次并绑定规则版本 |
| POST | /review/batches/{id}/works | 秘书 | 批次加入作品（仅草稿） |
| POST | /review/batches/{id}/exceptions | 秘书 | 发布前例外：coverage_waiver / load_cap_waiver（冲突不可豁免） |
| POST | /review/batches/{id}/plan | 秘书 | 冻结关系快照并生成可解释方案；约束不满足整体失败不留痕 |
| POST | /review/batches/{id}/publish | 秘书 | 发布批次 |
| GET | /review/my-tasks | 评委 | 只含本人匿名任务，未领取不返回材料地址 |
| GET | /review/tasks/{id}/material | 评委 | 领取后获取匿名材料；非本人/冲突任务统一返回 404 |
| POST | /review/tasks/{id}/claim | 评委 | 领取任务（BEGIN IMMEDIATE + 条件 UPDATE 防并发重复） |
| POST | /review/tasks/{id}/scores | 评委 | 提交评分（0–100），提交后不可修改 |
| POST | /review/tasks/{id}/recuse | 评委/秘书 | 临时回避；正式席位退出自动按候补位置递补 |
| POST | /review/tasks/{id}/invalidate | 秘书 | 作废评分：分数原样保留且不计入法定人数，触发递补 |
| POST | /review/batches/{id}/absences | 秘书 | 批次级缺席登记，批量结束未封作品上的任务并递补 |
| POST | /review/batches/{id}/emergency-assignments | 秘书 | 候补耗尽且正式席位不足时例外补位；实时冲突校验，红线不可突破 |
| POST | /review/batches/{id}/works/{wid}/seal | 秘书 | 达到 quorum 份有效评分才能封存作品 |
| POST | /review/batches/{id}/seal | 秘书 | 全部作品封存后封存批次 |
| GET | /review/batches/{id}/report | 纪检 | 复核每作品快照哈希、规则版本、回避原因、候补顺序、例外与任务事件链 |
| GET | /review/snapshots/{id} | 纪检 | 取冻结的关系快照原文并复算哈希 |

### 关键规则

- **匿名边界**：评委接口只返回匿名编号、赛道、必需专业与材料地址；机构、真实题名、团队人员永不下发。
- **快照与可解释性**：生成方案时把全部关系、评委档案与作品团队冻结为内容寻址快照（SHA-256）；方案逐人记录入选理由与排除原因（含具体冲突关系）。
- **回避红线**：利益冲突（顾问、任职、合作、亲属、师生、同属协会）只能排除，不能由例外覆盖；紧急替补仍做实时冲突校验。
- **只增事实**：任务状态、任务事件和分数均不修改不删除；作废仅翻转任务有效性，分数与哈希保留。封存后拒绝一切任务/分数调整。
- **并发与重启**：写事务在 SQLite 层串行化，领取与评分靠条件 UPDATE 保证唯一；全部状态持久化，重启后已领取任务与候补队列不变。
