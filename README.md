# 反兴奋剂检测与结果管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8301`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/eligibility.py`：报名资格判定（运动员本人 + 所属队伍 + 团队人员的有效处罚合并核对）。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制、审计写入，以及报名确认/失效/恢复流程。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：报名资格演示页面（可报名与被挡原因分栏）。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8301
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `athlete`：运动员（可通过`team_id`归属于队伍）；`team`：参赛队伍；`personnel`：团队人员（`role`为`doctor`或`coach`）。
- `sample`：检测样本；`case`：结果管理案件（主体`subject_kind`可为`athlete`/`team`/`personnel`）。
- `sanction`：禁赛处罚，由案件`decide=sanction`（需`start_date`/`end_date`）自动生成；申诉改判`no_sanction`时撤销。
- `registration`：赛事报名记录，状态`confirmed`/`invalidated`，原成绩`result`在失效后仍保留。

## 报名资格核对

资格判定、报名记录与接口入口分开维护：

- 确认报名时合并运动员本人、所属队伍和团队人员（队医/教练）的**有效**处罚；
  只要任一案件的禁赛区间覆盖比赛日期，就拒绝确认，响应为`409 EligibilityBlocked`，
  并在`items`中逐条列出处罚主体、案件、条款和起止日期。
- 案件判罚后自动核对已报名记录：被有效处罚覆盖比赛日期的报名标为`invalidated`，
  `result`原成绩保留；处罚期满（`end_date`已过）或经申诉撤销后，再次核对才恢复为`confirmed`。

主要报名接口：

- `GET /api/eligibility?event=<赛事>&event_date=<YYYY-MM-DD>`：判定结果，`eligible`与`blocked`（含`blocks`事项）分开返回。
- `POST /api/registrations/confirm`：报名确认闸门，请求体`{"athlete_id","event","event_date"}`，支持`Idempotency-Key`。
- `POST /api/registrations/reconcile`：对全部（或指定`event`）报名执行覆盖失效/期满恢复核对。
- `GET /api/registrations?event=<赛事>`：报名记录，含存储状态`status`、实时状态`effective_status`和`live_blocks`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤；支持`athletes`、`teams`、`personnel`、`samples`、`cases`、`sanctions`、`registrations`。
- `POST /api/<kind>`：创建对象；请求体为JSON（`sanction`与`registration`为流程托管，须走专用入口）。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
