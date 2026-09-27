# 反兴奋剂检测与结果管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8301`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/eligibility.py`：报名资格判定，合并运动员本人、所属队伍与团队人员的有效处罚。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制、处罚联动和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面，分开显示可报名名单与被挡原因。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8301
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `athlete`：运动员，可用`team_id`挂靠队伍；`sample`：检测样本；`case`：结果管理案件。
- `team`：队伍；`personnel`：团队人员（教练、队医等，挂靠队伍）。
- `sanction`：有效处罚，指向`athlete`/`team`/`personnel`任一主体，含`start_date`/`end_date`，可关联来源`case_id`。
- `event`：赛事，含`start_date`/可选`end_date`；`registration`：报名记录（`pending`→`confirmed`，另有`invalid`等状态，成绩存于`result`）。

## 报名资格核对

- 判定、报名记录和接口入口分开维护：判定逻辑在`src/eligibility.py`，报名记录走通用实体与`src/service.py`编排，接口入口为`/api/eligibility*`。
- 确认报名（`confirm`）时合并运动员本人、所属队伍及该队现役团队人员的有效处罚，命中任一处罚即拒绝确认，响应的`blockers`字段逐项列出命中事项。
- 处罚生效（新建`sanction`，或案件`decide`/`resolve_appeal`携带`ban_start`/`ban_end`自动生成）且覆盖比赛日期时，已确认报名自动置为`invalid`，`result`成绩保留。
- 处罚解除（`lift`）后自动恢复受影响的报名；处罚期满后通过`POST /api/eligibility/refresh`同步恢复，仍被覆盖的报名保持`invalid`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/eligibility?event_id=<id>[&athlete_id=<id>]`：报名资格判定；不传`athlete_id`时返回该赛事全部运动员的`registrable`与`blocked`（含被挡原因）两个名单。
- `POST /api/eligibility/refresh`：按当前有效处罚同步报名记录，请求体可带`event_id`或`registration_id`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
