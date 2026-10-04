# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位、授权和状态机，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --migrate --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。接口为 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/sources/sync`、`POST /api/items/<id>/claim`、`POST /api/items/<id>/actions`、`POST /api/admin/migrate` 和 `GET /api/items/<id>/audit`。

办理链：协调员 `POST /api/items/<id>/claim` 领取主办后形成唯一跟进人 `follow_up`，跨区同时提交时后到者收到 `409 already_claimed` 并在 `details.follow_up` 看到最新跟进人。测量更正（`correct_measurement`）后原评估立即失效并重算，已批准的停用授权清空、状态回到 `located`，需重新确认。断网期间的补测通过 `POST /api/items/<id>/sources/sync` 批量回网，按来源合并、重复只算一次；逐条独立提交，`confirmed` 保留已确认部分，`pending` 保留未完成项供重试。旧事件没有主办人时 `POST /api/admin/migrate`（或 `--migrate`）根据最近处置补出归属，历史审计保留不变。

测试覆盖完整调查流程、测量更正、重复事件、跨区越权、定位置信度、版本冲突、领取主办冲突、补测合并与部分失败重试、迁移补录。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。
