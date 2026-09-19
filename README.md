# 数字炸弹（Number Bomb）

多人在线「数字炸弹」小游戏的 Python 后端，内置一个零构建依赖的前端单页，开箱即可完整游玩。

**技术栈**：Python 3.10+ · FastAPI · WebSocket · uvicorn · 原生 HTML/CSS/JS

**核心特性**

- WebSocket 实时推送，全员状态严格一致（开始 / 关闭 / 轮转 / Boom 均为服务端驱动）
- 游戏规则与网络层完全解耦：`app/game.py` 为纯逻辑、零 IO，可脱离服务直接单测
- 炸弹数字在对局过程中**从不下发**，仅 Boom 时公布，前端无法作弊
- 断线自动重算轮次，全员掉线房间自动归零
- 109 个测试函数（参数化展开后 140 个用例），覆盖全部规则分支与端到端流程

---

## 目录

- [游戏规则](#游戏规则)
- [快速开始](#快速开始)
- [部署说明](#部署说明)
- [WebSocket 协议文档](#websocket-协议文档)
- [HTTP 接口](#http-接口)
- [配置项说明](#配置项说明)
- [项目结构](#项目结构)
- [运行测试](#运行测试)
- [重要限制](#重要限制)

---

## 游戏规则

### 基本流程

```
主界面 ──输入微信昵称──► 游戏大厅 ──任意玩家点开始──► 游戏进行中 ──有人踩雷──► Boom
   ▲                        │                                                    │
   │                        └──────────任意玩家点「关闭大厅」──────────┐            │
   └───────────────────────────────────────────────────────────────┴────────────┘
                                                        3 秒后回到大厅
```

### 1. 加入大厅

- 主界面点击「加入游戏大厅」，需先输入**微信昵称**（自动去首尾空格，不可为空，默认最长 20 字符）。
- **游戏进行中或 Boom 展示期间，新玩家点击加入会被拒绝**，提示「游戏进行中，暂时无法加入大厅」。
- 昵称默认**允许重复**（微信昵称本就可能重名）。玩家身份由服务端分配的 `playerId` 唯一确定；界面展示时重名会追加序号，如「小明(1)」「小明(2)」，以便区分。
- 重复点击加入是幂等的，不会把自己加两次。

### 2. 数字范围由人数决定

人数在**点击「开始游戏」的那一刻快照锁定**，游戏开始后的断线不再改变本局范围。

| 玩家人数 | 数字范围（闭区间） |
|---|---|
| 1 ~ 4 人 | **0 ~ 100** |
| 5 人及以上 | **0 ~ 1000** |

炸弹为该范围内的一个随机整数。

### 3. 游戏顺序与回合

- 玩家顺序 = **加入大厅的先后顺序**，界面玩家列表即游戏顺序（左侧序号标明位次）。
- 依次轮流，到末尾后回到第一位循环。
- **只有当前回合玩家**看到输入框，其余玩家看到「等待 XX 操作…」。
- 任意一名房间内玩家都可以点击「开始游戏」，不限于房主。

### 4. 猜数字与边界收缩

轮到某玩家时，界面显示「请输入从 `x` - `y`」并附带输入框。判定规则如下（`bomb` 为炸弹数字）：

| 输入情况 | 处理 |
|---|---|
| 不是整数（如 `abc`、`12.5`、空值） | 拒绝，提示「请输入一个整数」，**不消耗回合** |
| 越界（`< x` 或 `> y`） | 拒绝，提示「请输入 x - y 之间的整数」，**不消耗回合** |
| 非当前回合玩家提交 | 拒绝，提示「还没轮到你，请耐心等待」 |
| `guess == bomb` | **踩雷**，进入 Boom |
| `guess < bomb` | 安全，左边界抬升：`x = guess + 1`，轮到下一位 |
| `guess > bomb` | 安全，右边界压低：`y = guess - 1`，轮到下一位 |

**采用闭区间 + 严格收缩**：边界值 `x` 与 `y` 本身都是合法输入；每次安全猜测都让区间**至少缩小 1**。由此保证两条不变量：

1. 炸弹始终落在 `[x, y]` 内 —— 界面提示的范围与实际合法范围完全一致，不会出现「按提示输入却被告知非法」；
2. 游戏**必然在有限回合内结束**，不会死循环。

### 5. Boom 与回到大厅

- 玩家踩雷后，**所有玩家**同时看到 Boom 界面：`「玩家昵称」，Boom！`，并公布炸弹数字。
- 界面停留 **3 秒**（可配置）后，全员自动回到**游戏大厅**（不是主界面）。
- 回到大厅时：**玩家列表与顺序保留**，边界、炸弹、回合计数全部重置。
- 下一局再次点击「开始游戏」时，会**重新统计当前人数**并重选范围，仍从第一位玩家开始。

### 6. 关闭大厅

- 大厅内任意玩家点击「关闭大厅」，**所有人退回最初的主界面**，玩家列表清空。
- WebSocket 连接保持存活，玩家可立即重新输入昵称加入。
- 游戏进行中、Boom 展示期间同样可以关闭，作为异常卡死时的兜底出口；Boom 倒计时期间关闭会取消该定时任务，不会把状态又改回大厅。

### 7. 断线处理

| 场景 | 处理 |
|---|---|
| 非当前回合玩家断线 | 从玩家列表移除，当前回合不变 |
| **当前回合玩家断线** | 移除后**自动轮到下一位**，其余玩家立即收到新状态 |
| 断线者位于当前玩家之前 | 轮次索引左移，保证仍指向同一位玩家 |
| 末位玩家断线 | 索引取模，不越界 |
| 全员断线 | 房间自动回到主界面状态 |
| 前端页面刷新 / 网络抖动 | 客户端指数退避自动重连（1s → 8s 上限），并自动尝试归队 |

---

## 快速开始

### 环境要求

- **Python 3.10 或更高版本**（开发验证于 3.14，生产建议 3.12）
- 无需 Node.js —— 前端为零构建的原生单页

### 三步启动

```bash
# 1. 进入项目目录
cd number-bomb-game-Server

# 2. 创建虚拟环境并安装依赖（推荐）
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. 启动开发服务器
python run.py
```

浏览器打开 **http://localhost:8000** 即可游玩。

多人测试：在**同一台机器**上开多个浏览器标签页，或让**同一局域网**内的其他设备访问 `http://<你的内网IP>:8000`（`run.py` 默认监听 `0.0.0.0`，已允许外部访问）。

> 接口文档：启动后访问 http://localhost:8000/docs

---

## 部署说明

### ⚠️ 首要约束：必须单 worker

游戏状态保存在**进程内存**中，因此生产环境**只能以单 worker 运行**：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

若启动多个 worker，玩家会被操作系统分散到不同进程，导致**彼此完全看不见**（各自在独立的空房间里）。这不是配置疏漏，而是当前架构的固有约束。需要横向扩展时见 [重要限制](#重要限制)。

### 方式一：直接用 uvicorn（最简单）

```bash
cd /path/to/number-bomb-game-Server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 前台运行（便于观察日志）
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

# 后台运行
nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 > bomb.log 2>&1 &
```

### 方式二：systemd 托管（推荐用于长期运行）

创建 `/etc/systemd/system/number-bomb.service`：

```ini
[Unit]
Description=Number Bomb Game Server
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/number-bomb-game-Server
Environment="PATH=/opt/number-bomb-game-Server/.venv/bin"
ExecStart=/opt/number-bomb-game-Server/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now number-bomb
sudo systemctl status number-bomb      # 查看运行状态
journalctl -u number-bomb -f           # 跟踪日志
```

> 上例监听 `127.0.0.1`，配合下方 Nginx 反代对外提供服务；若要直接对外暴露，改为 `0.0.0.0`。

### 方式三：Docker

```bash
# 构建镜像
docker build -t number-bomb:1.0.0 .

# 运行（内置健康检查，30 秒探测一次 /api/health）
docker run -d --name number-bomb \
  -p 8000:8000 \
  --restart unless-stopped \
  number-bomb:1.0.0

# 查看日志
docker logs -f number-bomb
```

Dockerfile 已做安全加固：以非 root 用户 `bomb` 运行，先复制 `requirements.txt` 再复制代码以利用镜像层缓存，并固定 `--workers 1`。

如需自定义规则，通过环境变量注入即可（变量清单见 [配置项说明](#配置项说明)）：

```bash
docker run -d -p 8000:8000 \
  -e BOMB_BOOM_DURATION_MS=5000 \
  -e BOMB_RANGE_LARGE=0,2000 \
  number-bomb:1.0.0
```

### Nginx 反向代理（必须配置 WebSocket 升级）

WebSocket 依赖 HTTP `Upgrade` 握手，Nginx **必须显式转发 `Upgrade` 与 `Connection` 头**，否则连接会被降级为普通 HTTP 请求而失败（浏览器报 `Unexpected response code: 200` 或握手超时）。

```nginx
# 放在 http {} 块内，定义升级所需的映射
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name game.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;

        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;   # ← 关键，缺失则 WebSocket 无法握手
        proxy_set_header Connection $connection_upgrade;  # ← 关键

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 长连接读超时须大于客户端心跳间隔（前端每 25 秒 ping 一次）
        proxy_read_timeout  300s;
        proxy_send_timeout  300s;
    }
}
```

**HTTPS 场景**：页面通过 `https://` 访问时，前端会自动使用 `wss://`（见 `static/app.js` 的 `wsUrl()`），无需改代码。但需确保 Nginx 已配置证书，且 `proxy_set_header X-Forwarded-Proto $scheme` 存在。

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 部署后自检

```bash
# 1. 存活检查
curl http://localhost:8000/api/health
#    期望：{"status":"ok","phase":"idle","playerCount":0,"connections":0}

# 2. 核对生效的规则参数
curl http://localhost:8000/api/config

# 3. 检查前端页面可访问
curl -I http://localhost:8000/
#    期望：HTTP/1.1 200 OK

# 4. 跑一遍测试套件（需在服务器安装测试依赖）
pip install -r requirements.txt
pytest
```

---

## WebSocket 协议文档

面向需要对接**独立前端**的场景。内置前端已实现全部协议，可直接参考 `static/app.js`。

### 连接

```
ws://<host>:<port>/ws        # HTTP 页面
wss://<host>:<port>/ws       # HTTPS 页面
```

连接建立后，服务端**立即依次下发两条消息**：

1. `welcome` —— 分配 `playerId`
2. `state` —— 当前完整状态（使刷新页面的客户端能立即回到正确界面）

所有消息均为 JSON 对象，字段统一使用 **camelCase**。

### 客户端 → 服务端

| action | 附加字段 | 说明 |
|---|---|---|
| `join_lobby` | `nickname`: string | 加入大厅（或更新自己的昵称） |
| `start_game` | — | 开始游戏，任意房间内玩家可触发 |
| `close_lobby` | — | 关闭大厅，全员退回主界面 |
| `guess` | `value`: int \| string | 提交猜测；整数字符串（如 `"50"`）也可被正确解析 |
| `ping` | — | 心跳，服务端回 `pong` |

```json
{ "action": "join_lobby", "nickname": "小明" }
{ "action": "start_game" }
{ "action": "guess", "value": 50 }
{ "action": "close_lobby" }
{ "action": "ping" }
```

> `action` 大小写不敏感、自动去空格。未知 action 返回 `INVALID_ACTION`。

### 服务端 → 客户端

#### `welcome` —— 连接建立

```json
{ "type": "welcome", "playerId": "p_0001", "serverTime": 1760000000000 }
```

#### `state` —— 全局状态广播

每次状态变化推送给**所有**连接。`isYou` / `isYourTurn` / `you` 按接收方个性化计算。

```json
{
  "type": "state",
  "phase": "playing",
  "you": { "id": "p_0002", "nickname": "小红", "inRoom": true },
  "room": {
    "players": [
      { "id": "p_0001", "nickname": "小明", "online": true, "isYou": false },
      { "id": "p_0002", "nickname": "小红", "online": true, "isYou": true }
    ],
    "hostId": "p_0001"
  },
  "game": {
    "left": 11,
    "right": 100,
    "playerCount": 2,
    "turnCount": 3,
    "currentPlayerId": "p_0002",
    "currentPlayerNickname": "小红",
    "isYourTurn": true,
    "lastGuess": { "nickname": "小明", "value": 10, "result": "safe" },
    "boomNickname": null,
    "bomb": null
  },
  "serverTime": 1760000000000
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `phase` | `idle`（主界面）/ `lobby`（大厅）/ `playing`（游戏中）/ `boom`（踩雷展示） |
| `you.inRoom` | 当前连接是否已加入房间；`idle` 时为 `false` |
| `room` | 仅 `phase != idle` 时有值，否则为 `null` |
| `room.hostId` | 房主，即 `players[0]`（首个加入者） |
| `game` | 仅 `playing` / `boom` 时有值，否则为 `null` |
| `game.left` / `right` | 当前可猜的闭区间边界 |
| `game.playerCount` | 开局瞬间的人数快照 |
| `game.turnCount` | 本局累计的**合法**猜测次数（非法输入不计入） |
| `game.isYourTurn` | 是否轮到当前接收方；前端据此决定是否显示输入框 |
| `game.lastGuess.result` | `safe` 或 `boom` |
| `game.bomb` | **仅 `boom` 阶段公布**，其余阶段恒为 `null` |

#### `boom` —— 踩雷提示

```json
{
  "type": "boom",
  "nickname": "小红",
  "bomb": 42,
  "backToLobbyInMs": 3000
}
```

前端应展示「`nickname`，Boom！」并按 `backToLobbyInMs` 倒计时。倒计时结束后会另有一条 `phase: "lobby"` 的 `state` 广播，以服务端为准切换界面。

#### `error` —— 定向错误

只发给触发错误的连接，**不广播**。收到 error 时全局状态并未改变。

```json
{ "type": "error", "code": "OUT_OF_RANGE", "message": "请输入 0 - 100 之间的整数" }
```

| code | 含义 |
|---|---|
| `INVALID_NICKNAME` | 昵称为空、纯空格或超长 |
| `DUPLICATE_NICKNAME` | 昵称重复（仅在关闭「允许重名」配置时出现） |
| `GAME_IN_PROGRESS` | 游戏进行中或 Boom 期间，拒绝加入大厅 |
| `ROOM_FULL` | 房间人数已达上限（仅在设置了 `BOMB_MAX_PLAYERS` 时出现） |
| `NOT_IN_ROOM` | 尚未加入大厅就执行了 start / close / guess |
| `NOT_YOUR_TURN` | 还没轮到你 |
| `OUT_OF_RANGE` | 数字越界，`message` 中含当前实际边界 |
| `INVALID_NUMBER` | 不是合法整数（非数字、小数、布尔值等） |
| `INVALID_PHASE` | 当前阶段不允许该操作（如未开始就猜、Boom 期间继续猜） |
| `INVALID_ACTION` | 未知 action，或服务端处理异常 |
| `MALFORMED_MESSAGE` | 消息不是合法 JSON，或不是 JSON 对象 |

#### `pong` —— 心跳应答

```json
{ "type": "pong", "serverTime": 1760000000000 }
```

### 消息顺序注意事项

一次操作可能触发多条下行消息（例如踩雷会依次产生 `boom` → `state(phase=boom)` → 3 秒后 `state(phase=lobby)`）。客户端**不应假设收到的第一条消息就是某特定类型**，建议按 `type` 字段分发处理。内置前端的 `_read_until` 思路即如此。

---

## HTTP 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/` | 前端单页 |
| `WS` | `/ws` | 游戏主通信端点 |
| `GET` | `/api/health` | 存活检查，返回 `{status, phase, playerCount, connections}` |
| `GET` | `/api/state` | 只读状态快照，用于调试。**不会返回炸弹数字** |
| `GET` | `/api/config` | 当前生效的规则参数，便于部署后核对配置 |
| `GET` | `/docs` | FastAPI 自动生成的交互式接口文档 |
| `GET` | `/redoc` | 同上，ReDoc 风格 |

`/style.css`、`/app.js` 等静态资源由后端直接托管，无需另配静态服务器。

---

## 配置项说明

所有参数集中在 `app/config.py`，**均可通过环境变量覆盖，无需改代码**。解析失败时安全回退到默认值。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `BOMB_SMALL_ROOM_MAX_PLAYERS` | `4` | 人数阈值，`<=` 该值用小范围 |
| `BOMB_RANGE_SMALL` | `0,100` | 小范围数字区间，格式 `起,止` |
| `BOMB_RANGE_LARGE` | `0,1000` | 大范围数字区间，格式 `起,止` |
| `BOMB_BOOM_DURATION_MS` | `3000` | Boom 界面停留时长（毫秒） |
| `BOMB_NICKNAME_MAX_LEN` | `20` | 昵称最大字符数 |
| `BOMB_ALLOW_DUPLICATE_NICK` | `true` | 是否允许昵称重复（接受 `1/0`、`true/false`、`yes/no`） |
| `BOMB_MAX_PLAYERS` | 不限制 | 房间人数上限，设为 `0` 或负数表示不限制 |
| `BOMB_HOST` | `0.0.0.0` | 监听地址（仅 `run.py` 使用） |
| `BOMB_PORT` | `8000` | 监听端口（仅 `run.py` 使用） |

示例：把 Boom 停留延长到 5 秒、大范围改为 0-2000、昵称上限 12 字符：

```bash
BOMB_BOOM_DURATION_MS=5000 BOMB_RANGE_LARGE=0,2000 BOMB_NICKNAME_MAX_LEN=12 python run.py
```

---

## 项目结构

```
number-bomb-game-Server/
├── app/
│   ├── __init__.py
│   ├── main.py       # FastAPI 实例、路由与静态资源挂载、生命周期钩子
│   ├── config.py     # 可调参数集中管理（全部支持环境变量覆盖）
│   ├── models.py     # Pydantic 消息模型 + Phase / ErrorCode / ClientAction 枚举
│   ├── game.py       # ★ 纯游戏逻辑 GameRoom，零 IO，可独立单测
│   └── ws.py         # ConnectionManager（连接管理/广播）+ 消息分发路由
├── static/
│   ├── index.html    # 单页：主界面 / 大厅 / 游戏 / Boom 四态切换
│   ├── style.css
│   └── app.js        # WebSocket 客户端 + 渲染 + 断线自动重连 + 心跳
├── tests/
│   ├── conftest.py   # 共享夹具：路径注入、确定性炸弹、配置隔离
│   ├── test_game.py  # 规则单测（66 个用例）
│   └── test_ws.py    # WebSocket 端到端流程测试（43 个用例）
├── requirements.txt
├── pytest.ini
├── run.py            # 开发启动入口（含热重载）
├── Dockerfile
├── .gitignore
└── README.md
```

### 分层原则

`game.py` 与 `ws.py` **严格分离**：

- **`game.py`** —— 所有规则计算（范围选择、边界收缩、轮次推进、合法性校验、断线重算）都表现为对 `GameRoom` 的同步方法调用，不导入 FastAPI、不碰网络、不碰全局变量。业务错误统一抛 `GameError(code, message)`。
- **`ws.py`** —— 只负责收发、广播、连接生命周期与 Boom 定时任务，规则一律委托给 `game.py`。

好处是规则可以用 pytest 直接覆盖而无需起服务，且规则变更不会牵连网络层。

### 并发安全

`GameRoom` 的所有方法都是**同步且不含 `await`** 的。在 FastAPI 事件循环中，一次方法调用会原子执行完毕，不会被其他连接的请求打断，因此无需加锁。

> 维护约定：`ws.py` 中调用 `game_room.*` 的前后**不得插入 `await`**，否则会破坏原子性、引入竞态。

---

## 运行测试

```bash
pip install -r requirements.txt
pytest                    # 运行全部（配置见 pytest.ini）
pytest tests/test_game.py -v      # 仅规则单测，不需启动服务
pytest tests/test_ws.py -v        # 仅端到端测试
pytest -k "boom" -v               # 按关键字筛选
pytest --maxfail=1                # 首个失败即停止
```

### 覆盖范围

**`test_game.py`（66 个用例，纯逻辑）**

| 测试类 | 覆盖内容 |
|---|---|
| `TestRangeSelection` | 人数阈值定范围、4/5 人边界、快照锁定、下局重新快照 |
| `TestTurnOrder` | 首位起局、按加入顺序循环、任意玩家可开局 |
| `TestBoundaryShrink` | 边界抬升/压低、炸弹恒在区间内、边界值可猜、每回合至少缩 1、**200 次随机对局必然终止** |
| `TestInvalidInput` | 越界、非整数（7 种形态）、非当前玩家、非成员、未开局，均不推进回合 |
| `TestBoom` | 进入 Boom、炸弹事先不泄露、Boom 时公布、回大厅保留玩家、Boom 期间拒绝猜测 |
| `TestLobbyStateMachine` | 首人开厅、游戏中/Boom 期拒绝加入、关闭大厅回主界面、各阶段均可关闭、幂等重复加入 |
| `TestNicknameValidation` | 去空格、空值、超长、重名允许、重名序号展示、可配置禁止重名、房间满员 |
| `TestDisconnect` | 移除玩家、当前玩家断线跳轮、前/后位断线索引重算、索引取模、全员断线归零、未入厅断线静默 |
| `TestSnapshot` | 各阶段字段完整性、`isYou` / `isYourTurn` 按接收方个性化、camelCase 序列化 |
| `TestConfig` | 环境变量解析、非法值回退、默认配置自洽 |

**`test_ws.py`（43 个用例，真实 WebSocket 端到端）**

大厅流转与全员广播 · 游戏中/Boom 期拒绝加入 · 关闭大厅回主界面 · 各人数下的范围下发 · 加入顺序即游戏顺序 · 边界收缩与轮次推进的广播一致性 · 越界/非数字/非当前玩家的错误码 · 整数字符串容错 · Boom 全员提示与 3 秒回大厅 · Boom 期间关闭大厅取消定时任务 · 重名序号展示 · 当前玩家断线跳轮 · 前/后位断线的轮次保持 · 末位断线索引取模 · 全员断线房间归零 · 关闭后重新加入 · 未入厅连接断开不产生多余广播 · **0-100 与 0-1000 两局完整对局跑通到 Boom**

### 测试的两点技术约定

1. `app.ws.game_room` 是模块级单例，`conftest.py` 与 `reset_room` 夹具在每个用例前后重置，避免相互污染。
2. `TestClient` 必须以**上下文管理器**方式使用。否则每个 `websocket_connect` 会各自创建独立事件循环，而广播是跨连接操作，多连接场景会失败或挂起。测试中的 `Conn` 包装类还保证了关闭操作**幂等**——断线用例需主动关闭连接模拟掉线，而收尾时又会统一关闭全部连接，若不幂等则第二次关闭会因服务端已退出、无人消费而永久阻塞。

---

## 重要限制

### 只能单 worker，暂不支持横向扩展

游戏状态存于进程内存，因此：

- **必须** `--workers 1`，多 worker 会导致玩家分散到不同进程、互相不可见；
- **不能**多实例部署（多容器 / 多机器），各实例的房间彼此独立；
- 进程重启会**丢失所有进行中的对局**，玩家需重新加入；
- 单进程可支撑的并发连接数有限，适合中小规模（数十至数百人）场景。

若需突破，二期改造方向是引入 **Redis**：用 Redis Hash 存房间状态替代进程内存，用 Redis Pub/Sub 跨进程转发广播消息。届时 `game.py` 的纯逻辑设计可以基本保留，主要改动集中在 `ws.py` 的状态读写与广播通道。

### 其他说明

- **无鉴权**：昵称由玩家自行填写，服务端不校验真实性，也不做微信授权对接。如需防刷可加限流或接入微信登录。
- **单房间**：同一时刻只存在一个大厅 / 一局游戏，不支持多房间并行。
- **无持久化**：不记录历史战绩，进程重启即清零。
- **CORS 全开**：`main.py` 中 `allow_origins=["*"]` 便于前后端分离联调，生产环境若前端与后端同源可收紧。

---

## 反馈与二次开发

调整规则只需改 `app/game.py`（逻辑）或 `app/config.py`（参数），改完跑一遍 `pytest tests/test_game.py` 即可验证。协议变更需同步修改 `app/models.py` 与 `static/app.js`。
