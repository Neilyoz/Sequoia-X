# T7 · Session 登录鉴权与静态托管（后端）

**依赖**：T3、T5　**阻塞**：T8
**规模**：2 个新文件 + 改 3 个文件 + 2 个测试文件
**纯后端任务**：不写一行前端代码，T8 才建 `frontend/`。

## 1. 目标

把 T3 的"只认 `X-API-Key`"升级成"session cookie 或 API Key 二者之一"，
让浏览器不必持有密钥；同时把 Next.js 静态产物挂到同一个 FastAPI 应用上，
使生产环境只需一个进程。

## 2. 动手前先读

- [03-frontend-and-auth.md](../03-frontend-and-auth.md) **全文**（§3 会话、§4 CSRF、§5 托管坑）
- 约束 [§3](../01-constraints.md)（单进程假设）、[§7](../01-constraints.md)（配置兼容）
- T3 实际代码：`sequoia_x/api/deps.py`、`app.py`（`include_router` 顺序）、
  `routes/system.py` 里 `/health` 是怎么免鉴权的
- T5 实际代码：`routes/queries.py` 的 router 注册方式（保持一致，别造第二套风格）

## 3. 文件清单

新建：
- `sequoia_x/api/auth.py` —— `SessionStore`、登录校验、`authenticate()`
- `sequoia_x/api/routes/auth.py` —— `/api/auth/login|logout|me`
- `tests/test_api_auth_session.py`、`tests/test_static_hosting.py`

修改：
- `sequoia_x/api/deps.py` —— 路由依赖从 `require_api_key` 换成 `require_auth`
- `sequoia_x/api/app.py` —— ① lifespan 里建 `SessionStore` 挂 `app.state`；
  ② 注册 auth router；③ **在所有 router 注册之后**追加静态挂载
- `sequoia_x/core/config.py` + `.env.example` —— 新增会话与静态目录配置
- `sequoia_x/api/routes/system.py` —— `/api/info` 增加鉴权模式字段

禁止改动：`sequoia_x/task/**`、`sequoia_x/runner/**`、`sequoia_x/scheduler/**`、
`main.py`、`data/**`、`sequoia_x/api/routes/queries.py`（除依赖名替换，见 §4.2）。
**不创建 `frontend/` 目录**（T8 的事）。

## 4. 实现要点

### 4.1 配置新增（约束 §7，全部有默认值）

```python
session_ttl_seconds: int = 604800          # 7 天
frontend_dist_path: str = "frontend/out"   # 静态产物目录
serve_frontend: bool = True                # 找不到产物时自动降级为"只提供 API"
```

`.env.example` 注释要写清：`API_KEY` 是**唯一登录凭据**，泄露等于交出服务控制权；
留空则整个鉴权层失效（T3 的 WARNING 行为）。

### 4.2 `auth.py`

```python
class SessionStore:
    """进程内会话表。单进程部署前提，见 03-frontend-and-auth.md §3。"""
    def issue(self, ttl_seconds: int) -> str                 # secrets.token_urlsafe(32)
    def is_valid(self, token: str) -> bool                   # hmac.compare_digest 比对
    def revoke(self, token: str) -> None
    def purge_expired(self) -> int                           # 登录时顺手调
    def __len__(self) -> int                                 # 供 /api/info 观测

def authenticate(request, settings, store, api_key_header) -> AuthContext:
    """按 03 §3.4 的四步顺序判定。返回 AuthContext(mode='open'|'session'|'apikey')。"""

def require_auth(...)  # 依赖：不通过抛 HTTPException(401, detail={"code":"unauthorized"})
def require_write_guard(request, auth: AuthContext) -> None:
    """cookie 模式下的写操作必须带 X-Sequoia-Client: web（03 §4 第 2 道防线）。"""
```

- **`authenticate()` 是判定逻辑的唯一实现处**（03 §3.4）。
  T3 的 `require_api_key` 内部改为调用它，保持向后兼容；
  路由侧只见 `require_auth`。**不要再在别处写第四种判定分支。**
- 会话表必须挂 `app.state`，**不用模块级全局**（T3 已确立此约定，否则第二个测试 app 串味）。
- 401 响应体不得包含：正确 key 的任何片段、token、过期时间。统一
  `{"detail":{"code":"unauthorized"}}`；登录失败用 `{"detail":{"code":"invalid_credentials"}}`
  —— **两个码要不同**，前端据此决定"跳登录页"还是"提示密码错"。
- 写操作守卫的豁免清单：`POST /api/auth/login` 不要求 `X-Sequoia-Client`。
  豁免方式要显式（依赖参数 `write_guard=False`），不要靠 `if request.url.path == ...` 字符串判断。

### 4.3 `routes/auth.py`

- `login`：body `{"api_key": str}`（pydantic 模型，不要 `dict`）。
  校验用 `hmac.compare_digest(body.api_key, settings.api_key)`。
  成功 → 200 + `Set-Cookie`（`HTTPOnly`、`SameSite=strict`、`Path=/`、`Max-Age=ttl`），
  body 回 `{"authenticated": true, "expires_in": ttl}`。
- **`API_KEY` 未配置时 `login` 返回 400 `{"detail":{"code":"auth_disabled"}}`**，
  并说明"服务处于无鉴权模式，无需登录"。（否则前端会卡在"输入什么 key 都失败"的困惑里。）
- `me`：通过 `require_auth`；返回 `{"authenticated": true, "mode": auth.mode}`。
  `mode=open` 时前端跳过登录页，这是本地开发体验的关键。
- `logout`：从 cookie 取 token → `revoke` → 下发过期 cookie → 204。
  无 cookie 也返回 204（幂等，别给前端制造 401）。
- **限流**：`login` 失败按客户端 IP 计数，1 分钟内 5 次失败后返回 429。
  实现成 `SessionStore` 旁边的一个简单 dict 即可，**不要引入 slowapi 等新依赖**。
  理由：`API_KEY` 是长随机串，但服务可能被暴露；暴力尝试要有闸。
  这个 dict 也是进程内的，重启清零 —— 可接受，注释说明。
- cookie 名常量 `SESSION_COOKIE_NAME = "sequoia_session"`，定义在 `auth.py` 供测试导入。

### 4.4 静态挂载（本任务最容易出事的地方）

在 `app.py` 里，**位置必须在所有 `include_router` 与异常处理器注册之后**：

```python
def mount_frontend(app: FastAPI, settings: Settings, logger) -> None:
    """把 Next.js 静态导出产物挂到根路径。找不到目录时只警告，不报错。"""
    dist = Path(settings.frontend_dist_path)
    if not dist.is_dir():
        logger.warning(f"未找到前端产物 {dist}，仅提供 API 服务（开发期正常）")
        return
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    logger.info(f"前端静态资源已挂载：{dist}")
```

- 必须在 T8 还没建 `frontend/` 时也能正常启动 —— 所以"目录不存在 → 警告并跳过"是硬要求。
  `serve_frontend=False` 时同样跳过。
- **`/api/*`、`/health`、`/docs`、`/openapi.json` 不能被遮蔽**。
  Starlette 的路由匹配按注册顺序，`mount("/")` 放最后即可；
  但这条太隐蔽，**必须有测试**（见 §5），不能只靠注释提醒后人。
- `StaticFiles` 只在 `dist` 存在时挂，所以启动日志是唯一能看出"为什么根路径 404"的线索，
  日志文案要写清后续动作建议（"如需页面，执行 cd frontend && npm run build"）。
- 不要为静态资源加鉴权（页面本身无信息量），**但 `index.html` 也不带数据**，
  所以放开是安全的，且能避免"未登录连登录页都打不开"的死锁。

### 4.5 `/api/info` 追加字段

```json
{"...原字段": "",
 "auth_mode": "apikey" | "disabled",
 "active_sessions": 3,
 "frontend_served": true}
```

`auth_mode` 与 `frontend_served` 是排查"为什么我打不开页面/为什么不用登录"的第一现场信息。

## 5. 测试要求

`tests/test_api_auth_session.py`
- 正确 key 登录 → 200、`Set-Cookie` 含 `HttpOnly`、`SameSite=strict`、`Max-Age` 等于配置值。
- 错 key → 401 `invalid_credentials`，响应体不含 key 片段（`assert settings.api_key not in resp.text`，
  **这条防泄露，必须写**）。
- 登录后带 cookie 访问 `/api/tasks` → 非 401；删 cookie → 401。
- 带 `X-API-Key` 直接访问（不登录）→ 200，且**不需要** `X-Sequoia-Client` 头。
- cookie 模式下 `POST /api/tasks/daily` 缺 `X-Sequoia-Client: web` → 403
  `{"code":"missing_client_header"}`；带上 → 202。
- `logout` 后原 cookie 立刻失效。
- 过期会话：手工把 token 的 `expires_at` 改到过去 → 访问返回 401，且 `purge_expired()` 回收。
- 限流：连续 5 次错 key 后第 6 次返回 429（`login` 计数按 IP）。
- `API_KEY` 为空配置：所有 `/api/*` 免鉴权可用；`login` 返回 400 `auth_disabled`；
  `/api/auth/me` 返回 `mode="open"`。
- `SessionStore` 单测：`issue` 返回的 token 唯一、`is_valid` 对不存在/已撤销/过期都 False。

`tests/test_static_hosting.py`
- 造一个临时 `dist` 目录（写 `index.html`、`login/index.html`），
  `create_app(settings 带 frontend_dist_path=tmp)`：
  - `GET /` → 200 且返回 index.html 内容
  - `GET /login/` → 200（验证 `trailingSlash` + `html=True` 的组合行为，
    **顺带确认 03 §5.2 的结论**，若与文档不符回来更新文档）
  - `GET /health` → 200 **且不是 index.html**（守 §4.4 的遮蔽风险）
  - `GET /api/tasks` → 401/200，不是 HTML
  - `GET /openapi.json` → 是 JSON
- 目录不存在 → 应用照常启动、`/health` 200、`/` 404、日志有 WARNING（`caplog` 断言）。
- `serve_frontend=False` → 同上。

## 6. 验收标准

```bash
ruff check sequoia_x
pytest -q
# 手工（不依赖前端）
uvicorn sequoia_x.api.app:app --port 8123
curl -i -s -X POST http://127.0.0.1:8123/api/auth/login \
     -H 'Content-Type: application/json' -d '{"api_key":"<真实key>"}' | grep -i set-cookie
curl -i -s http://127.0.0.1:8123/api/tasks                      # 401
curl -s -c jar -X POST .../login -d '...'; curl -s -b jar http://127.0.0.1:8123/api/tasks   # 200
```

逐条自查：

1. T3 原有测试**全部不修改仍然通过**（`tests/test_api_auth.py` 若必须改，
   只允许改"期望状态码/错误码"这类契约明确变更处，且要在汇报里逐条列出理由）。
2. `grep -rn "compare_digest" sequoia_x/api/` 覆盖 key 与 token 两处比较。
3. `grep -rn "SessionStore()" sequoia_x/api/` 无模块级实例化（只在 lifespan 里建）。
4. `data/sequoia_v2.db` mtime 与哈希未变。
5. 没建 `frontend/` 目录（`git status` 确认）。
6. 无新增 Python 依赖（`git diff pyproject.toml` 只应有 `.env` 无关内容或为空）。

## 7. 完成后汇报

- `Set-Cookie` 实际字面值（脱敏后）与浏览器行为差异说明
- Starlette `mount("/")` 与 `/api` 匹配的实测结论（`/docs`、`/openapi.json` 各自结果）
- `StaticFiles(html=True)` 对 `/login` 与 `/login/` 的差异实测 —— T8 要据此定 `trailingSlash`
- 限流实现的计数粒度（IP？还是全局？）与你选的取舍
- 改了哪些 T3 测试、为什么
