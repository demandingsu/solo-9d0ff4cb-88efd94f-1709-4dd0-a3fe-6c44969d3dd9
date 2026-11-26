# 社区乒乓球赛 · Flask 裁判台

一个可直接用 Docker Compose 启动的社区乒乓球赛裁判 Web 台：Flask + 服务端渲染 + SQLite，
无外部服务、无前端构建。内置 A、B 两组各三人的单循环赛程（共六场），支持整表录入/更正、
自动排名、半决赛对阵、按场记分、球台对调、撤场封存、同两人重新配对、旧页面交分裁决，
并在两场现行半决赛都有比分后才显示决赛选手（另附轻量决赛/冠军）。

> **重复排人防护（本次修复重点）**：同一名球员不能再被排进两场现行半决赛；给空台重配时
> 若该球员已在另一场现行半决赛，提交会被退回并指出冲突场次（JSON 409
> `sf_player_conflict`，表单 302 定位到 `/#match-<冲突场次>`）。“查重 + 占台 + 插入”在
> `BEGIN IMMEDIATE` 串行写事务中完成，两个裁判同时给两张空台排同一人也只有一份成功。
> 对库里**已经存在**的重复赛程，网页会用红色横幅/卡片/行内标记标出球员与场次，比分照常可查；
> 冲突期间暂停晋级、决赛记分与冠军展示；撤下重复场合并补齐两场合规赛果后，按有效胜者重建
> 决赛，原先据冲突赛程产生的决赛比分以 `sf_conflict` 原因封存、历史页可查。

---

## 1. 快速开始

### 方式 A：Docker Compose（交付方式）

```bash
docker compose up --build
# 打开 http://localhost:5000
# 若宿主机 5000 端口被占用：
PORT=8080 docker compose up --build   # 然后访问 http://localhost:8080
```

- SQLite 文件位于命名卷 `pingpong-sqlite-data` 内的 `/data/referee.db`，重启/重建容器数据不丢。
- 容器入口会自动建表并播种 6 名球员、6 场小组赛和初始半决赛对阵。
- 自带 `/healthz` 健康检查。

### 方式 B：本地 Python 运行

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

python wsgi.py
# 或：DB_PATH=./data/referee.db gunicorn --bind 127.0.0.1:5000 --preload wsgi:app
```

### 跑测试 / 黑盒试跑

```bash
pip install pytest
pytest -q                                   # 25 个行为/并发测试

# 先起服务，再跑纯标准库黑盒剧本（对调球台 + 撤场重配 + 旧页交分）
DB_PATH=/tmp/trial.db gunicorn --bind 127.0.0.1:5555 --preload wsgi:app &
python scripts/e2e_trial.py http://127.0.0.1:5555

# 重复排人拦截 + 已有冲突的网页标注/冻结 + 撤场恢复/旧决赛封存（33 项）
python scripts/e2e_conflict_recovery.py http://127.0.0.1:5555 \
    --db-path /tmp/trial.db     # --db-path 会注入旧 bug 脏赛程（清空 matches 表）

# 两个裁判并发给两张空台排同一人（默认 8 轮，每轮都断言只有一份成功）
python scripts/e2e_concurrent_repair.py http://127.0.0.1:5555 --rounds 10
```

#### 用 Docker Compose 做冲突/并发试跑

```bash
PORT=5000 docker compose up --build -d
# 冲突 + 恢复（脚本已 COPY 进镜像；直接打容器内端口与 /data 下的库）
docker compose exec referee python3 scripts/e2e_conflict_recovery.py \
    http://127.0.0.1:5000 --db-path /data/referee.db
# 并发重配
docker compose exec referee python3 scripts/e2e_concurrent_repair.py \
    http://127.0.0.1:5000 --rounds 10
# 或手动注入同一份脏赛程用于在浏览器里查看标注：
docker compose exec referee python3 scripts/dev_inject_conflict.py
```

> 注意：`e2e_conflict_recovery.py --db-path` 与 `dev_inject_conflict.py` 会
> **清空 `matches` 表**并写入固定的冲突演示数据，只能用于一次性试跑库，
> 不要对正式裁判库执行。

---

## 2. 赛制与排名（严格按需求实现）

- 内置球员与抽签号（抽签号全局唯一，页面可见）：

  | 组 | 抽签号 / 球员 |
  |---|---|
  | A | 1 陈一鸣、2 林浩然、3 赵子轩 |
  | B | 4 周雨桐、5 吴俊杰、6 郑晓峰 |

- 每组三人单循环，固定 3 场，两组合计 **6 场**，首页一张整表一次提交/更正。
- 排名键只有两条，顺序固定：**胜场数降序 → 抽签号升序**
  （代码见 `app/tournament.py::compute_standings`，不掺入小分/相互战绩）。
- 半决赛默认：**一号台 A1 对 B2（SF1）**、**二号台 B1 对 A2（SF2）**。
- 小组整表提交后，半决赛对阵立即按最新排名调和更新。

---

## 3. 核心身份模型：场次 ≠ 球台 ≠ 球员对

这是“换台比分照旧 / 撤下可查 / 重配重新记分 / 旧页面交分裁决”的关键。
淘汰赛每一次“配对发生”都是 `matches` 表中一条独立记录，拥有稳定自增 `id`：

| 操作 | 对数据做了什么 | 旧页面交分结果 |
|---|---|---|
| **对调球台** | 只交换两条 active 记录的 `table_no`，`score1/score2` 原样不动 | 旧表单 POST 到固定的 `/matches/<id>/score`，场次仍 active → **接受** |
| **撤下对阵** | 记录置 `status='withdrawn'`，比分、时间、原因全部保留，历史页/旧链接可查 | 旧 `id` 已是 withdrawn → **HTTP 409 + “请刷新”提示**（JSON 返回 `code=match_withdrawn`） |
| **同两人重新配对** | 插入一条**全新 id、空白比分**的 active 记录；绝不继承旧比分 | 旧 id 仍 withdrawn → **继续拒绝**；新 id 页面正常记分 |

数据库还有两个部分唯一索引兜底业务不变量：同一轮次同一对球员至多一条现行场次、
同一轮次同一张球台至多一条现行场次。

### 小组重新整表提交时，已有半决赛怎么办？

调和逻辑（`app/services.py::reconcile_semifinals`）按“球员对”匹配：

- 球员对仍属于 A1-B2 / B1-A2：保留同一条场次，**已录比分和球台都保留**；
- 球员对已不在最新对阵中：
  - 从未记分的自动空壳 → 删除，避免历史噪音；
  - 已记分或人工重配场次 → 标记 `withdrawn / lineup_update`，比分封存可查，再生成空白新场。
- 一旦现行半决赛本身存在“同一球员在两场 / 自己对自己”的重复排人冲突，调和直接跳过、
  保留现场供裁判撤场处理（见下节），不会用自动对阵把冲突痕迹覆盖掉。

### 重复排人防护、冲突标注与恢复

旧版只防“同一对球员重复”和“同一张台重复”，没防“**同一名球员出现在两场现行半决赛**”，
于是裁判撤下一场、给空台重配时，可以把另一场仍在打的球员再排一次；该球员若两场都赢，
还会生成 `player1_id == player2_id` 的“自己和自己打”的决赛。修复后的规则：

1. **写入拦截**：`create_repair()` 在 `BEGIN IMMEDIATE` 写事务内依次查
   “同对现行场次 → 任一球员已在另一场现行 SF → 球台占用”，命中球员冲突即抛
   `PlayerAlreadyScheduledError`，文案与 JSON `conflicts` 都带冲突场次 `#id`/球台/对阵。
   写锁把并发“查重 + 插入”串行化，后到的裁判一定看得到先提交的排人；另有
   `uq_active_pair` / `uq_active_table` 两个部分唯一索引兜底。
2. **存量脏赛程只标注、不崩页、不自动抹掉**：`semifinal_conflict_report()` 找出
   “同一球员在 ≥2 场不同现行 SF”和“自己对自己”两类冲突。首页 `#sf-conflicts` 红色横幅、
   冲突卡片红框、`⛔ 重复排人` 徽标、球员名红底标记；历史页同样有横幅与行标记。
   所有已录比分原样渲染、可查。检测到冲突时 `reconcile_semifinals()` 不做自动调和，
   保留现场（重启也不会把冲突痕迹悄悄改掉）。
3. **冲突期间冻结晋级**：`finalists_state.ready=False` 且 `conflict=True`、不返回胜者；
   首页显示 `final-suspended`，不显示决赛记分表单与冠军；`champion()` 也加了冲突闸门。
   直接 `POST /matches/<决赛id>/score` 会被 **409 `semifinal_conflict`** 退回；若该决赛仍
   active，其独立页面用 `final-score-blocked` 面板替换记分表单。
4. **冲突决赛封存**：`reconcile_final()` 的冲突分支把 active 且有比分的决赛置
   `withdrawn / sf_conflict`（空白空壳删除），比分保留；裁判撤下冲突 SF 时也会先做同样的
   封存，保证历史页的封存原因准确。
5. **恢复**：撤下重复 SF → 在空台补齐合规对阵（或重新整表提交触发调和）→ 两场都有有效
   比分后，`reconcile_final()` 按两位有效胜者生成 pair 不同的空白新决赛；旧冲突决赛保持
   withdrawn，比分与“半决赛重复排人冲突，决赛暂停/封存”原因在历史页/旧链接可查。

新增机器码：`sf_player_conflict`（重配被退回，409）、`semifinal_conflict`
（冲突期间决赛交分被退回，409）；`GET /api/state` 新增 `sf_conflicts` 与
`finalists.conflict/conflicts` 字段。

---

## 4. 页面与 HTTP / JSON 接口

页面（服务端渲染，中文界面，关键元素带 `data-testid`）：

| 页面 | 地址 |
|---|---|
| 裁判台（排名 + 六场整表 + 球台 + 决赛选手） | `GET /` |
| 某场次的固定“旧页面”（active 可记分；withdrawn 封存/提示刷新/重配） | `GET /matches/<id>` |
| 历史对阵与撤场记录、通用重新配对 | `GET /history` |
| 完整 JSON 状态 | `GET /api/state` |
| 健康检查 | `GET /healthz` |

主要写接口（表单与 JSON 同源；`/api/...` 返回 JSON，表单提交走 PRG 跳转/错误页）：

| 行为 | 表单/页面 | JSON |
|---|---|---|
| 六场比分整表提交 | `POST /groups/scores`（字段 `score_<gmId>_p1/p2`） | `POST /api/groups/scores`，body `{"scores":{"1":[3,1], …}}` |
| 按场登记/更正比分 | `POST /matches/<id>/score`（`score1`,`score2`） | `POST /api/matches/<id>/score` |
| 旧球台页交分（支持 `match_id` 或 `player1_id/player2_id`/`pair_key` 解析） | `POST /tables/<n>/score` | 同名，JSON |
| 对调两张球台 | `POST /semifinals/swap-tables` | `POST /api/semifinals/swap-tables` |
| 单场换台 | `POST /matches/<id>/table` | — |
| 撤下对阵 | `POST /matches/<id>/withdraw` | `POST /api/matches/<id>/withdraw` |
| 原班两人重新配对 | `POST /matches/<id>/re-pair`（兼容别名 `/repait`） | `POST /api/matches/<id>/repait` |
| 任意两人新建配对 | `POST /pairings` | 同名，JSON |

另提供语义别名 `POST /semifinals/<id>/score`、`POST /semifinal/<id>/score`、`POST /swap-tables`。

比分规则：两格必须同时填写；允许 0–999 的整数（可填局分或小分）；
乒乓球必须分胜负，**双方相等会被拒绝**并给出中文错误（小组整表为原子提交，任一场不合法则六场都不入库）。

决赛选手判定：现行半决赛**恰好两场、两场所属球员互不重复（无冲突）且两场都有可分胜负的比分**时，
`#finalists` 区域才显示两位决赛选手；人数不足显示 `已完成 x/y` 与原因，存在重复排人时显示
`final-suspended` 冲突暂停说明。决赛选手产生后会出现轻量决赛记分与冠军横幅（加分项），
但冲突期间决赛入口/记分/冠军一律冻结（见上节）。

---

## 5. 收尾核对：做成了什么、实际测了什么、还有什么没做

### 5.1 做成了什么（对照原始要求）

- ✅ Flask 裁判台 + SQLite 持久化，Docker Compose 可启动（含命名卷与健康检查）。
- ✅ 内置 A/B 各三人单循环，固定六场；首页一张整表一次录入或更正，原子校验。
- ✅ 排名严格按“胜场降序 → 同胜场抽签号升序”，页面同时展示胜/负/已赛/得失分。
- ✅ 半决赛默认一号台 A1-B2、二号台 B1-A2；整表提交后对阵调和更新。
- ✅ 半决赛按场登记/更正；另有固定 URL 的单场页面。
- ✅ **对调球台后比分跟随球员/场次原样保留**，旧场次页/旧首页卡片继续可交分。
- ✅ **撤下记录与比分在历史页和旧链接中仍可查**。
- ✅ **同两人重新配对生成全新空白场次，重新记分**；重复的现行同对配对会被 409/提示拦截。
- ✅ **已撤下场次的旧页面交分被退回 HTTP 409，并明确提示“刷新页面”**（HTML 与 JSON 双形态）。
- ✅ 两场现行半决赛都有比分后才显示决赛选手；未满足时显示进度与原因。
- ✅ **修复重复排人**：重配时若所选球员已在另一场现行半决赛，立即退回并指出冲突场次
  （JSON `sf_player_conflict` + `conflict_match_ids/conflicts`；表单 302 到 `/#match-<id>`
  并 flash 冲突说明），从入口杜绝“一人两场 SF / 自己和自己打决赛”。
- ✅ **并发重配安全**：`BEGIN IMMEDIATE` 串行写事务 + `busy_timeout` + 部分唯一索引，
  两个裁判同时给两张空台排同一人只有一份成功（有自动化线程测试与 HTTP 并发剧本）。
- ✅ **存量重复赛程网页标注**：首页/历史页红色横幅列出“球员 ↔ 冲突场次（球台）”，
  卡片红框、徽标、球员名高亮；两场 SF 比分与历史比分照常可查。
- ✅ **冲突期间冻结晋级/决赛记分/冠军**：即使两场胜者不同也不产生决赛名单；
  决赛交分 409 `semifinal_conflict`，决赛页记分表单替换为暂停面板，冠军不展示。
- ✅ **恢复闭环**：撤下重复场 → 补齐合规对阵与两场合规赛果 → 按有效胜者生成空白新决赛；
  据冲突赛程产生的旧决赛以 `sf_conflict` 封存，比分 `4:3` 等在历史页/旧链接可查，之后新决赛
  可正常记分并产生冠军。
- ✅ 额外：单场换台、通用配对、决赛记分/冠军、完整 JSON API、**25 个 pytest 用例**、
  三套纯标准库黑盒剧本（原有 32 项 + 冲突恢复 33 项 + 并发 44 项）与脏赛程注入脚本。

### 5.2 实际测了什么（都是本环境真实执行，非仅阅读代码）

环境说明：本沙箱 `python3` 无 pip/Flask，已用 `get-pip.py --target=/tmp/pylibs`
把 `Flask 3.1.3 / gunicorn 27.0.1 / pytest 9.2.0` 装到独立目录并以
`PYTHONPATH=/tmp/pylibs:/workspace` 运行；所有服务均为真实进程。

- ✅ `python -m pytest -q`：**25 passed**。在原 15 个用例全绿的基础上新增 10 个：
  重配撞人 JSON 409 且 `conflict_match_ids` 指向正确场次、表单 302 锚点 + flash、
  withdrawn 源场次重配同样查重复、脏赛程标注与比分可见、冲突冻结晋级/决赛/冠军、
  决赛 GET 面板与 HTML 409 `conflict-block-warning`、撤场恢复 + 旧决赛封存 + 新决赛冠军、
  **重启 create_app 后脏赛程仍在且冲突决赛被 `sf_conflict` 封存**、
  **两线程 Barrier 并发 `create_repair` 恰好一成一拒且球员只在一场 SF**、
  不存在/已撤场次的交分与换台表单不再 500。
- ✅ `scripts/e2e_trial.py` 对真实 **gunicorn（2 workers + 4 threads + `--preload`）
  + 文件 SQLite(WAL)**：**32/32 通过**，原有换台/撤场/重配/旧页 409/决赛选手流程无回归。
- ✅ `scripts/e2e_conflict_recovery.py --db-path <db>`：同一真实 gunicorn 上
  **33/33 通过**，完整走了题目要求的网页试跑：
  干净库重配撞人被退回且文案含 `#场次`、HTML 302 到 `/#match-<id>` →
  注入旧 bug 脏赛程（#1 `1-5` 与 #3 `1-3` 重复排球员 1，两场胜者分别为 1、3，
  决赛 #4 `1-3 (4:3)`）→ `/api/state` 冲突结构正确、首页/历史页冲突横幅与
  `conflict-player-1`/`conflict-match-1/3`/徽标齐全、SF 比分 `3:1`、`1:3` 仍渲染 →
  `final-suspended` 与无冠军块 → 决赛交分 409 `semifinal_conflict` 且 `5:4` 未写入、
  #4 以 `sf_conflict` 封存保留 `4:3` → 网页撤下 #3 → API 补齐 `4-2` 并记 `2:3` →
  有效胜者 `{1,2}`、生成 `1-2` 空白决赛、#4 仍封存、历史页可见 `4 : 3` →
  新决赛 `3:2` 后冠军陈一鸣、首页冠军块恢复。
- ✅ `scripts/e2e_concurrent_repair.py --rounds 10`：对真实 2-worker gunicorn
  发起真 HTTP 并发，**44/44 通过**：每轮两个线程用 Barrier 同时请求
  “1+3 排一号台 / 1+6 排二号台”，**每轮恰好 1 个 200、另 1 个 409
  `sf_player_conflict` 且指出获胜场次**，轮后轮询 `/api/state` 不变量
  “没有球员在 >1 场现行 SF、无冲突标记”恒成立；另含一组完全相同配对的并发对照（一成一 409）。
- ✅ 额外加压：自写 5 轮 × 6 裁判同时提交（同球员、同台/异台混合），每轮仍恰好 1 份成功、
  球员 1 只出现在 1 场 SF；落败方为 `sf_player_conflict` 或同对 `duplicate_active_pair`。
- ✅ 试跑服务器日志：最终一轮全套验证期间 **0 Traceback、0 HTTP 500**。
- ✅ 实跑 `docker-entrypoint.sh` 的初始化（临时 `DB_PATH`），确认生成
  6 球员 / 6 小组赛 / 2 场 SF；`sh -n` 入口脚本语法通过。
- ✅ 用 PyYAML 解析校验 `docker-compose.yml`（服务/端口/命名卷正确），用 JSON 解析校验
  Dockerfile exec-form `CMD`（11 个 argv）；所有 Python 文件 `py_compile` 通过。

### 5.3 还有什么没做 / 已知边界（如实说明）

- ⚠️ **本沙箱没有 docker / podman / nerdctl，也没有 `/var/run/docker.sock`，
  当前 uid 非 root、`apt-get` 无法加锁，因此无法在这里真正
  `docker compose build && docker compose up`**。已做到的最接近验证：
  静态校验 Compose/Dockerfile、实跑 entrypoint 播种，并用与镜像 CMD **完全相同**的
  `gunicorn --bind … --workers 2 --threads 4 --preload wsgi:app`（同样的文件 SQLite+WAL）
  跑了上述全部 HTTP/并发剧本。镜像内 `pip install` 与首次拉取 `python:3.12-slim` 未实跑
  （Flask/gunicorn 已在本机从 PyPI 成功安装验证）；在有 Docker 的机器上按文首
  “用 Docker Compose 做冲突/并发试跑”的命令即可复跑，若网络受限可能需要配置 pip 镜像源。
- 并发保证面向“所有写入都经过本应用”的部署形态：靠 `BEGIN IMMEDIATE` 串行化
  `create_repair` 的查重/占台/插入，外加 pair/table 两个部分唯一索引；**没有**再为
  “球员在 SF 出现至多一次”加数据库表达式唯一索引或触发器——因为存量脏数据必须允许存在并被
  网页标注，唯一索引会在脏库上创建失败、触发器则可能在冲突未撤前连比分更正也拦掉。
  若有绕过应用直接写 SQLite 的运维脚本，需要自行遵守该不变量（或先停服务再写）。
- 未做登录、裁判身份与权限控制（定位为社区内网裁判台，拿到页面即可操作），因此“两个裁判”
  的并发测试不区分账号，只验证提交互斥。
- 比分只记每场两个总数，未记录逐局小分、暂停、黄牌等明细。
- 小组赛六场是固定行原地更新，**不保留每次修改的历史版本**；淘汰赛的撤下/对阵更新则有状态、
  原因与时间戳可追溯，但也不是完整审计日志。
- “旧页面”裁决依据是稳定场次 id 与 active/withdrawn 状态；未实现乐观锁版本号。
- 决赛/冠军为轻量加分项；未做季军赛、签表导出、打印海报、CSV 导出等。
- 没有浏览器级/视觉自动化测试（环境无 Playwright/Node 测试链），页面为服务端渲染，
  已用真实 HTML 内容断言替代视觉检查；仅含确认框等少量原生 JS；未做多语言（仅中文）。
