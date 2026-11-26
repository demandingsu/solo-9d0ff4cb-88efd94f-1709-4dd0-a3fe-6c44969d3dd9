#!/usr/bin/env python3
"""黑盒试跑：重复排人拦截 → 已有冲突的网页标注/冻结 → 撤场恢复 → 旧决赛封存。

仅用标准库。推荐对真实 gunicorn（多 worker + 线程）运行：

    DB_PATH=/tmp/trial_referee.db \
    gunicorn --bind 127.0.0.1:5566 --workers 2 --threads 4 --preload wsgi:app
    python3 scripts/e2e_conflict_recovery.py http://127.0.0.1:5566 \
        --db-path /tmp/trial_referee.db

带 --db-path 时，脚本会直接用 sqlite3 注入“旧版本 bug”留下的重复排人脏赛程
（会清空 matches 表，仅适用于一次性试跑库）。Docker Compose 用法见 README。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

BASE = (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--")
        else "http://127.0.0.1:5000").rstrip("/")
DB_PATH: str | None = None
if "--db-path" in sys.argv:
    DB_PATH = sys.argv[sys.argv.index("--db-path") + 1]

failures: list[str] = []
passed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failures.append(f"{name} {detail}")
        print(f"  ❌ {name} {detail}")


def call(method: str, path: str, *, form=None, body=None):
    data = None
    headers = {}
    if form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        BASE + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def call_form_no_redirect(path: str, form: dict):
    data = urlencode(form).encode()
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=15) as r:
            return r.status, r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


def get_state(retries: int = 1):
    for _ in range(retries):
        status, text = call("GET", "/api/state")
        if status == 200:
            return json.loads(text)
        time.sleep(0.3)
    raise AssertionError("GET /api/state failed")


INJECT_SQL = """
DELETE FROM matches;
DELETE FROM sqlite_sequence WHERE name = 'matches';
INSERT INTO matches
  (id, round, bracket_code, table_no, player1_id, player2_id,
   status, pair_key, created_at, withdrawn_at, withdraw_reason, score1, score2)
VALUES
  (1, 'SF', 'SF1',    1, 1, 5, 'active',    '1-5', '2026-01-01T00:00:00+00:00', NULL, NULL, 3, 1),
  (2, 'SF', 'SF2',    2, 4, 2, 'withdrawn', '2-4', '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00', 'manual', NULL, NULL),
  (3, 'SF', 'REPAIR', 2, 1, 3, 'active',    '1-3', '2026-01-03T00:00:00+00:00', NULL, NULL, 1, 3),
  (4, 'F',  'F1',  NULL, 1, 3, 'active',    '1-3', '2026-01-04T00:00:00+00:00', NULL, NULL, 4, 3);
"""


def inject_dirty() -> None:
    assert DB_PATH, "需要 --db-path 才能注入脏赛程"
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        conn.executescript(INJECT_SQL)
        conn.commit()
    finally:
        conn.close()
    # 等服务端连接看到新提交
    for _ in range(20):
        st = get_state()
        if st["sf_conflicts"]["has_conflict"]:
            return
        time.sleep(0.25)


def main() -> int:
    print(f"▶ 冲突/恢复黑盒试跑：{BASE}"
          + (f"（DB 注入：{DB_PATH}）" if DB_PATH else "（不注入，假定已是脏赛程）"))

    status, _ = call("GET", "/healthz")
    check("健康检查 200", status == 200, str(status))

    # ------------------------------------------------------------------ #
    # 阶段 1：干净赛程下，重配已在另一场 SF 的球员 -> 退回并指出冲突场次
    # （自建前置状态：撤空全部 SF，再在一号台放一场 1-5，二号台留空）
    # ------------------------------------------------------------------ #
    print("\n— 阶段 1：重配重复排人拦截 —")
    st = get_state()
    for m in st["active_semifinals"]:
        status, _ = call("POST", f"/api/matches/{m['id']}/withdraw", body={})
        if status != 200:
            check("撤空现有半决赛以建立前置状态", False, str(status))
    status, text = call("POST", "/pairings",
                        body={"player1_id": 1, "player2_id": 5, "table_no": 1})
    try:
        m1 = json.loads(text)["new_match_id"]
    except (json.JSONDecodeError, KeyError):
        m1 = None
    check("建立前置场次：一号台 1 对 5", status == 200 and isinstance(m1, int),
          f"status={status} body={text[:200]}")

    status, text = call("POST", "/pairings",
                        body={"player1_id": 1, "player2_id": 3, "table_no": 2})
    ok_json = False
    cid = None
    try:
        j = json.loads(text)
        ok_json = (status == 409 and j.get("code") == "sf_player_conflict")
        cid = (j.get("conflict_match_ids") or [None])[0]
        msg = j.get("message", "")
    except (json.JSONDecodeError, AttributeError):
        msg = text[:200]
    check("JSON 并排同一球员 -> 409 sf_player_conflict", ok_json,
          f"status={status} body={text[:200]}")
    check("错误指出的冲突场次正是仍 active 的一号台场次", cid == m1,
          f"cid={cid} m1={m1}")
    check("错误文案含冲突场次号 #%d" % m1, f"#{m1}" in msg, msg)

    code, location = call_form_no_redirect(
        "/pairings", {"player1_id": "6", "player2_id": "1", "table_no": "2"})
    check("HTML 表单提交被 302 退回并定位到冲突场次",
          code == 302 and location.endswith(f"#match-{m1}"),
          f"code={code} location={location}")

    st = get_state()
    check("被拒后没有产生重复排人/冲突标记",
          st["sf_conflicts"]["has_conflict"] is False
          and len(st["active_semifinals"]) == 1)

    # ------------------------------------------------------------------ #
    # 阶段 2：注入历史脏赛程，网页标注 + 冻结
    # ------------------------------------------------------------------ #
    print("\n— 阶段 2：已有重复赛程的标注与冻结 —")
    if DB_PATH:
        inject_dirty()
        print("  · 已注入旧 bug 脏赛程：#1(1-5) 与 #3(1-3) 重复排球员 1，"
              "决赛 #4(1-3, 4:3)")

    st = get_state(retries=5)
    rep = st["sf_conflicts"]
    check("/api/state 报告冲突：球员 1 在 #1、#3",
          rep["has_conflict"] and rep["player_ids"] == [1]
          and rep["match_ids"] == [1, 3], str(rep)[:300])
    fs = st["finalists"]
    check("冲突期间 finalists.ready=False / conflict=True，且不给出胜者",
          fs["ready"] is False and fs.get("conflict") is True
          and fs["winners"] == [])
    check("冲突期间 champion=None", st["champion"] is None)

    status, home = call("GET", "/")
    check("首页 200 且有冲突横幅",
          status == 200 and 'data-testid="sf-conflict-banner"' in home)
    check("首页标出冲突球员 testid=conflict-player-1",
          'data-testid="conflict-player-1"' in home)
    check("首页标出冲突场次 #1/#3",
          'data-testid="conflict-match-1"' in home
          and 'data-testid="conflict-match-3"' in home)
    check("首页冲突卡片有冲突徽标",
          'data-testid="conflict-badge-3"' in home)
    check("首页两场半决赛比分仍可查（#1 3:1，#3 1:3）",
          'data-testid="score-1-1">3<' in home
          and 'data-testid="score-3-2">3<' in home)
    check("首页显示决赛暂停面板 final-suspended",
          'data-testid="final-suspended"' in home)
    check("首页不展示冠军块", 'data-testid="champion"' not in home)

    status, hist = call("GET", "/history")
    check("历史页有冲突横幅与冲突行标注",
          status == 200
          and 'data-testid="history-conflict-banner"' in hist
          and 'data-testid="history-conflict-match-3"' in hist
          and 'data-conflict="1"' in hist)
    check("历史页仍可查两场冲突半决赛比分 3 : 1 / 1 : 3",
          "3 : 1" in hist and "1 : 3" in hist)

    # 冲突期间给旧决赛 #4 交分 -> 409，决赛被封存，比分不动
    status, text = call("POST", "/api/matches/4/score",
                        body={"score1": 5, "score2": 4})
    try:
        code = json.loads(text).get("code")
    except json.JSONDecodeError:
        code = None
    check("冲突期间决赛交分 -> 409 semifinal_conflict",
          status == 409 and code == "semifinal_conflict",
          f"status={status} body={text[:200]}")
    st = get_state()
    f4 = next(m for m in st["matches"] if m["id"] == 4)
    check("旧决赛 #4 已封存(sf_conflict)且尝试的 5:4 未写入，保留 4:3",
          f4["status"] == "withdrawn"
          and f4["withdraw_reason"] == "sf_conflict"
          and (f4["score1"], f4["score2"]) == (4, 3), str(f4))

    status, page4 = call("GET", "/matches/4")
    check("旧决赛页展示封存盒与冲突封存原因",
          status == 200 and 'data-testid="sealed-box"' in page4
          and "半决赛重复排人冲突" in page4)

    # ------------------------------------------------------------------ #
    # 阶段 3：撤场 + 补齐两场合规赛果 -> 有效胜者决赛，旧决赛封存可查
    # ------------------------------------------------------------------ #
    print("\n— 阶段 3：撤场恢复与决赛重建 —")
    status, _ = call("POST", "/api/matches/3/withdraw", body={})
    check("撤下重复场次 #3 成功", status == 200, str(status))

    status, text = call("POST", "/pairings",
                        body={"player1_id": 4, "player2_id": 2, "table_no": 2})
    new_sf = None
    try:
        new_sf = json.loads(text).get("new_match_id")
    except json.JSONDecodeError:
        pass
    check("在空出的二号台补齐合规半决赛 4 对 2",
          status == 200 and isinstance(new_sf, int), text[:200])

    status, text = call("POST", f"/api/matches/{new_sf}/score",
                        body={"score1": 2, "score2": 3})
    ready = False
    try:
        ready = json.loads(text)["finalists"]["ready"]
    except (json.JSONDecodeError, KeyError):
        pass
    check("新场记分 2:3（球员 2 胜）后 finalists.ready=True",
          status == 200 and ready, text[:200])

    st = get_state()
    check("冲突标记解除", st["sf_conflicts"]["has_conflict"] is False)
    winners = {w["player"]["id"] for w in st["finalists"]["winners"]}
    check("有效胜者为球员 1 与 2", winners == {1, 2}, str(winners))
    fnew = st["final_match"]
    check("按有效胜者生成 1-2 的空白新决赛",
          bool(fnew) and fnew["status"] == "active"
          and {fnew["player1_id"], fnew["player2_id"]} == {1, 2}
          and fnew["score1"] is None and fnew["score2"] is None,
          str(fnew))
    f4 = next(m for m in st["matches"] if m["id"] == 4)
    check("原冲突决赛 #4 仍封存且比分 4:3 保留",
          f4["status"] == "withdrawn"
          and (f4["score1"], f4["score2"]) == (4, 3))

    status, hist = call("GET", "/history")
    check("历史页可查封存的原决赛比分 4 : 3",
          status == 200 and "4 : 3" in hist)

    status, _ = call("POST", f"/api/matches/{fnew['id']}/score",
                     body={"score1": 3, "score2": 2})
    check("新决赛记分 3:2 成功", status == 200, str(status))
    st = get_state()
    champ = (st.get("champion") or {}).get("player", {})
    check("冠军恢复为 陈一鸣(1)", champ.get("id") == 1, str(st.get("champion")))
    status, home = call("GET", "/")
    check("首页展示冠军块与姓名",
          status == 200 and 'data-testid="champion"' in home
          and "陈一鸣" in home)

    return summary()


def summary() -> int:
    print()
    if failures:
        print(f"❌ {len(failures)} 项失败 / {passed} 项通过：")
        for f in failures:
            print("  -", f)
        return 1
    print(f"🎉 全部 {passed} 项冲突/恢复黑盒检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
