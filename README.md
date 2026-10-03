# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/recalc.py`：补发批次重算链——批次规范化、冲突挂起、来源顺序合并、年度累计与处置结论推导。
- `src/repository.py`：SQLite建表、事务、版本控制、审计链和重算链持久化（持久收件箱、读数、年度累计、结论版本）。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败和重算链测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`
- `POST /api/dose-batches`：补发剂量批次（支持单对象或`{"batches":[...]}`，dosimetrist / radiation_officer）。
- `POST /api/dose-batches/recover`：写入失败后从最后确认批次之后重放收件箱，已确认批次幂等跳过、不重复计入。
- `POST /api/doses/legacy`：导入旧数据（可缺`year_key`，dosimetrist）。
- `POST /api/doses/backfill-years`：缺少年度键的旧读数按测量时刻回填年度键并重算（dosimetrist）。
- `GET /api/doses`：查看批次、读数、年度累计和当前处置结论（所有角色）。

批次类型由`batch_type`区分：`normal`常规补发、`correction`人员更正、`recalibration`仪器复校正。重算链规则：

1. **同批次重传沿用首次结果**：相同`batch_no`直接返回首次结论（含挂起结论）并记`replayed`审计，不重复入账。
2. **重叠读数不一致整批挂起**：批内或与库内在效读数的同一`(person_id, period)`剂量不等时，全批置`suspended`，同批其他读数也不入账，仅记录冲突明细。
3. **同人员周期并发提交按来源顺序合并**：`lab_direct > dosimetrist > radiation_officer > manual`，剂量一致则折叠为一条在效读数，来源链写入`provenance`。
4. **事件更新后旧结论立即失效并重算**：更正/复校正先将旧读数置失效（回填`superseded_by`），再重算年度累计；旧结论标记`is_current=0`并生成新的调查、随访与报告期限。
5. **写入失败从最后确认批次恢复**：批次先入持久`ingest_inbox`（pending），处理成功才签收；`recover`重放pending载荷，批次已落库则补签收，绝不重复计入。
6. **旧数据年度键回填**：导入时缺`year_key`的读数不入年度累计，回填时按`measured_at`所属年份补键后重算。

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平（年度默认20mSv，随访阈值1.5倍）之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
