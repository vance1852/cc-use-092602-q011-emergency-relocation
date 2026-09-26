# 建设灾害预警下的应急转移协同基础平台

本项目是一套可离线运行的 Python 服务端平台，供县、乡镇和村级工作人员管理新型城镇化安置、土地资源分配、危房安全勘察与改造复核。账号登录、角色权限、业务状态、幂等结果和审计事件保存在 SQLite 中，适合安置经办、自然资源、住建复核与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/rural_allocation/`：乡镇片区、地块资源池、土地批次、家庭申请、分配运行与移交情景；
- `src/housing_safety/`：危房勘察协议、测量导入、异常复核、分析任务租约和安全结论；
- `src/remediation_review/`：改造案件、现场测量、风险分析、账号登录与质量审批；
- `src/evacuation/`：汛期地质灾害预警版本与风险曲线、村级人口基线、家庭可转移属性、
  分批撤离候选组合（车辆调度、临时床位、家庭集合点）、负责人确认一次性冻结资源、
  预警修订只重排未执行阶段、现场回执按事件时间归并并保护已确认终态、返迁计划与解释查询；
- `fixtures/`：离线验收使用的勘察协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

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
PYTHONPATH=src python3 -m rural_allocation.acceptance --workspace .
PYTHONPATH=src python3 -m housing_safety.acceptance --workspace .
PYTHONPATH=src python3 -m remediation_review.acceptance
PYTHONPATH=src python3 -m evacuation.acceptance --workspace .
```

四条命令使用临时 SQLite 数据库完成村镇与地块登记、家庭申请分配、危房测量分析、改造审批，
以及预警登记、候选组合确认冻结、乱序回执归并、预警修订与返迁的应急转移闭环，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m rural_allocation.api --database rural.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m housing_safety.api --database housing.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m remediation_review.api --database remediation.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m evacuation.api --database evacuation.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。

## 应急转移协同模块（evacuation，端口 8083）

角色：`risk`（预警与道路状态）、`planner`（人口基线与方案生成/修订）、`director`（负责人确认）、
`dispatcher`（现场回执）、`auditor`（查询与审计）。所有写接口通过 `X-Actor-Id` 识别操作人。

- `POST /warnings`、`GET /warnings/{id}`：预警版本（必须接续）、承诺转移窗口、返迁窗口与风险变化曲线；
- `POST /villages|households|vehicles|shelters|routes`：村级人口基线和家庭可转移属性
  （危房、行动不便、在校儿童、特殊医疗、集合点、是否需要车辆）、车辆（含轮椅位）、临时床位（含医护床位）、疏散道路；
- `POST /routes/{id}/status`、`GET /routes/{id}/status?as_of=`：道路 open/restricted/closed 事件按生效时间归并；
- `POST /plans`：一次生成三个确定性候选组合（safety_first / fast_clearance / shelter_balanced），
  含两批撤离（重点人群与危房户优先）、车辆车次与拼车、临时床位峰值占用、返迁错峰计划；
  硬约束包含重点人群优先、道路通行能力与封闭、车辆/轮椅位容量、床位与医护床位、承诺窗口内到达；
- `POST /plans/confirm`：仅负责人可确认，确认前复核资源竞争，确认后一次性冻结车辆时间窗与床位窗口
  （幂等键防重复提交），`GET /plans/{id}` 与 `/plans/{id}/candidates/{cid}` 查询方案与明细；
- `POST /plans/revise` + `POST /plans/reconfirm`：预警修订后只释放并重新规划尚未执行的阶段，
  已执行阶段作为只读前缀拼接，已占用车辆与床位继续受保护；
- `POST /receipts`：现场回执（notified/departed/arrived/exempt/returned），乱序按 `observed_at`
  归并、重复回执幂等标记、已确认终态（到达/豁免/返迁）不被中途状态回退；
- `GET /plans/{id}/explain`：解释实际转移人数、在途/未通知/未分配家庭及原因、各村影响、返迁计划与已返迁明细；
- `GET /audit/chain`：哈希链审计校验。
