# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位、授权和状态机，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --migrate --db ./data.db   # 为旧事件按最近处置补出主办归属
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。接口为 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/sources/batch`、`POST /api/items/<id>/actions` 和 `GET /api/items/<id>/audit`。

办理链要点：

- 同一台站、频点、时间视为同一干扰事件，跨区重复提交返回 409，`details` 中带有既有记录 id、状态和最新跟进人，后到者改为补充测量来源。
- 协调员通过 `claim` 动作领取主办，形成唯一跟进人；他人可再 `claim` 接管（审计记录上一跟进人）。已有跟进人时，处置类动作（`suspend`/`confirm_suspend`/`coordinate`/`resolve`/`cancel`）只能由跟进人或监管角色执行。
- `correct_measurement` 后原评估立即失效并重算（修订记录保留新旧评估）；若已批准停用授权，授权置为 `pending_reconfirm`，`coordinate`/`resolve` 被阻断，须 `confirm_suspend` 重新确认。
- 断网期间的补测回网后走 `sources/batch` 按来源合并：同一来源只记一次，每条独立写入，失败条目保留错误信息，整体可安全重试。
- `--migrate` 为没有主办人的旧事件按最近一次处置动作补出归属，并追加 `owner_backfilled` 审计事件，历史审计链保持可校验。

测试覆盖完整调查流程、测量更正与授权重确认、重复事件、跨区越权、定位置信度、版本冲突、领取与接管、批量来源合并重试和归属迁移。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。
