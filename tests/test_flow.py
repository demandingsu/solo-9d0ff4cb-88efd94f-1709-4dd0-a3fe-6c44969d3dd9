"""端到端行为测试：排名、整表提交、换台、撤下、重配、旧页面交分、决赛选手。"""
import re
import sqlite3
import threading

import pytest

from app import create_app
from app.db import connect
from app.services import PlayerAlreadyScheduledError, create_repair


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "referee_test.db"
    app = create_app({"TESTING": True, "DB_PATH": str(db_path)})
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def submit_tie_group_scores(client):
    """A 组三人各胜 1 场 -> 全部 1 胜，必须按抽签号 1,2,3 排名。

    1 胜 2（3:0）；3 胜 1（3:1）；2 胜 3（3:1）。
    B 组：4 两胜（3:1,3:0），5 胜 6（3:1）=> 4,5,6。
    """
    data = {
        "score_1_p1": "3", "score_1_p2": "0",
        "score_2_p1": "1", "score_2_p2": "3",
        "score_3_p1": "3", "score_3_p2": "1",
        "score_4_p1": "3", "score_4_p2": "1",
        "score_5_p1": "3", "score_5_p2": "0",
        "score_6_p1": "3", "score_6_p2": "1",
    }
    return client.post("/groups/scores", data=data)


def state(client):
    return client.get("/api/state").get_json()


# --------------------------------------------------------------------------- #
# 基础数据 / 排名
# --------------------------------------------------------------------------- #

def test_seed_has_six_group_matches(client):
    st = state(client)
    assert len(st["group_matches"]) == 6
    assert [p["draw_no"] for p in st["standings"]["A"]] == [1, 2, 3]
    assert [p["draw_no"] for p in st["standings"]["B"]] == [4, 5, 6]


def test_standings_wins_then_draw_number(client):
    resp = submit_tie_group_scores(client)
    assert resp.status_code == 302

    st = state(client)
    a = st["standings"]["A"]
    # 三人都是 1 胜 1 负，按抽签号升序
    assert [(r["id"], r["wins"], r["rank"]) for r in a] == [
        (1, 1, 1), (2, 1, 2), (3, 1, 3)
    ]
    b = st["standings"]["B"]
    assert [(r["id"], r["wins"]) for r in b] == [(4, 2), (5, 1), (6, 0)]


def test_semifinal_pairing_rules_after_submit(client):
    submit_tie_group_scores(client)
    st = state(client)
    t1, t2 = st["tables"]["1"], st["tables"]["2"]
    # 一号台 A1(1) 对 B2(5)；二号台 B1(4) 对 A2(2)
    assert {t1["player1_id"], t1["player2_id"]} == {1, 5}
    assert {t2["player1_id"], t2["player2_id"]} == {4, 2}
    assert t1["table_no"] == 1 and t2["table_no"] == 2
    assert t1["status"] == t2["status"] == "active"


def test_group_form_renders_all_six_inputs(client):
    html = client.get("/").get_data(as_text=True)
    assert 'data-testid="group-score-form"' in html
    for gid in range(1, 7):
        assert f'name="score_{gid}_p1"' in html
        assert f'name="score_{gid}_p2"' in html


def test_invalid_group_table_is_rejected_atomically(client):
    data = {
        "score_1_p1": "3", "score_1_p2": "3",  # 平分非法
        "score_2_p1": "1", "score_2_p2": "3",
    }
    resp = client.post("/groups/scores", data=data)
    assert resp.status_code == 400
    assert "不能相同" in resp.get_data(as_text=True)
    # 数据库没有写入任何比分
    for gm in state(client)["group_matches"]:
        assert gm["score1"] is None and gm["score2"] is None

    js = client.post("/api/groups/scores",
                     json={"scores": {"1": [5, 5]}})
    assert js.status_code == 400
    assert js.get_json()["error"] == "validation_error"


# --------------------------------------------------------------------------- #
# 决赛选手显示条件
# --------------------------------------------------------------------------- #

def test_finalists_only_after_both_semifinals_scored(client):
    submit_tie_group_scores(client)
    st = state(client)
    assert st["finalists"]["ready"] is False
    home = client.get("/").get_data(as_text=True)
    assert "finalists-pending" in home

    m1 = st["tables"]["1"]["id"]
    m2 = st["tables"]["2"]["id"]

    r = client.post(f"/api/matches/{m1}/score", json={"score1": 3, "score2": 1})
    assert r.status_code == 200
    assert r.get_json()["finalists"]["ready"] is False

    # 只填一边 -> 400
    bad = client.post(f"/api/matches/{m2}/score", json={"score1": 3})
    assert bad.status_code == 400

    r = client.post(f"/api/matches/{m2}/score", json={"score1": 1, "score2": 3})
    assert r.status_code == 200
    body = r.get_json()
    assert body["finalists"]["ready"] is True
    winner_ids = {w["player"]["id"] for w in body["finalists"]["winners"]}
    # m1 胜者 1；m2 (p4 vs p2) 客队 p2 3:1 胜 -> 2
    assert winner_ids == {1, 2}

    home = client.get("/").get_data(as_text=True)
    assert "finalist-names" in home
    assert "陈一鸣" in home and "林浩然" in home


# --------------------------------------------------------------------------- #
# 换台：同一对球员换台，比分照旧；旧页面仍可交分
# --------------------------------------------------------------------------- #

def test_swap_tables_scores_follow_pair_and_old_page_works(client):
    submit_tie_group_scores(client)
    st = state(client)
    m1, m2 = st["tables"]["1"]["id"], st["tables"]["2"]["id"]

    # 用“旧页面”的表单交分（独立场次页）
    page = client.get(f"/matches/{m1}").get_data(as_text=True)
    assert f'action="/matches/{m1}/score"' in page
    assert client.post(f"/matches/{m1}/score",
                       data={"score1": "3", "score2": "1"}).status_code == 302

    # 对调球台
    assert client.post("/semifinals/swap-tables").status_code == 302
    st = state(client)
    a = next(m for m in st["active_semifinals"] if m["id"] == m1)
    b = next(m for m in st["active_semifinals"] if m["id"] == m2)
    assert a["table_no"] == 2 and b["table_no"] == 1
    # 已录比分仍然挂在原场次（这对球员）上
    assert (a["score1"], a["score2"]) == (3, 1)

    # 旧页面（固定 /matches/<id>）继续交分/更正 -> 有效
    resp = client.post(f"/matches/{m1}/score",
                       data={"score1": "3", "score2": "2"})
    assert resp.status_code == 302
    a = next(m for m in state(client)["active_semifinals"] if m["id"] == m1)
    assert (a["score1"], a["score2"]) == (3, 2)
    assert a["table_no"] == 2  # 没有因为交分而弹回旧台

    # 旧的“一号台球台页”带隐藏 match_id 交分也应被接受（场次仍 active）
    resp = client.post("/tables/1/score",
                       data={"match_id": str(m1), "score1": "3", "score2": "0"})
    assert resp.status_code == 302
    a = next(m for m in state(client)["active_semifinals"] if m["id"] == m1)
    assert (a["score1"], a["score2"]) == (3, 0)


def test_swap_api_and_move_table(client):
    submit_tie_group_scores(client)
    st = state(client)
    m1, m2 = st["tables"]["1"]["id"], st["tables"]["2"]["id"]
    client.post(f"/api/matches/{m1}/score", json={"score1": 3, "score2": 2})

    # 两张台都有比赛时，手动换到被占球台 -> 400
    r = client.post(f"/matches/{m1}/table", data={"table_no": "2"})
    assert r.status_code == 302  # flash 错误后回跳
    st = state(client)
    assert next(m for m in st["active_semifinals"] if m["id"] == m1)["table_no"] == 1

    # 撤下 m2 后 m1 可以换到 2 号台，比分保留
    client.post(f"/matches/{m2}/withdraw")
    r = client.post(f"/matches/{m1}/table", data={"table_no": "2"})
    assert r.status_code == 302
    m = next(x for x in state(client)["active_semifinals"] if x["id"] == m1)
    assert m["table_no"] == 2 and (m["score1"], m["score2"]) == (3, 2)


# --------------------------------------------------------------------------- #
# 撤下 / 历史可查 / 重配 / 旧页面被拒并提示刷新
# --------------------------------------------------------------------------- #

def test_withdraw_history_repair_and_stale_old_page_rejected(client):
    submit_tie_group_scores(client)
    st = state(client)
    m1 = st["tables"]["1"]["id"]
    m2 = st["tables"]["2"]["id"]

    client.post(f"/matches/{m1}/score", data={"score1": "3", "score2": "2"})

    # 撤下
    assert client.post(f"/matches/{m1}/withdraw").status_code == 302
    st = state(client)
    old = next(m for m in st["matches"] if m["id"] == m1)
    assert old["status"] == "withdrawn"
    assert (old["score1"], old["score2"]) == (3, 2)
    assert st["tables"]["1"] is None
    assert st["finalists"]["ready"] is False

    # 历史页可查比分
    hist = client.get("/history").get_data(as_text=True)
    assert f'data-testid="history-row-{m1}"' in hist
    assert "3 : 2" in hist

    # GET 旧页面：封存信息 + 刷新/重配入口
    page = client.get(f"/matches/{m1}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "sealed-box" in html and "重新配对" in html

    # 用旧页面交分 -> 409 退回并提示刷新（HTML）
    resp = client.post(f"/matches/{m1}/score",
                       data={"score1": "9", "score2": "9"})
    assert resp.status_code == 409
    body = resp.get_data(as_text=True)
    assert "stale-warning" in body and "刷新" in body

    # JSON 客户端同样 409 + 机器可读代码
    js = client.post(f"/api/matches/{m1}/score", json={"score1": 5, "score2": 3})
    assert js.status_code == 409
    assert js.get_json()["code"] == "match_withdrawn"

    # 旧比分没有被那次被拒请求改动
    old = next(m for m in state(client)["matches"] if m["id"] == m1)
    assert (old["score1"], old["score2"]) == (3, 2)

    # 原班两人重新配对 -> 新 id、空白比分
    resp = client.post(f"/matches/{m1}/re-pair", data={"table_no": "1"})
    assert resp.status_code == 302
    new_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    assert new_id != m1
    new_m = next(m for m in state(client)["matches"] if m["id"] == new_id)
    assert new_m["status"] == "active"
    assert new_m["score1"] is None and new_m["score2"] is None
    assert {new_m["player1_id"], new_m["player2_id"]} == {1, 5}
    assert new_m["table_no"] == 1

    # 旧页面仍然被拒；新场次页面可以正常记分
    assert client.post(f"/matches/{m1}/score",
                       data={"score1": "1", "score2": "3"}).status_code == 409
    ok = client.post(f"/api/matches/{new_id}/score",
                     json={"score1": 3, "score2": 0})
    assert ok.status_code == 200

    # 重复给同一对球员建现行场次 -> 409
    dup = client.post(f"/api/matches/{m1}/repait", json={})
    assert dup.status_code == 409
    assert dup.get_json()["error"] == "duplicate_active_pair"

    # 另一场也记分后，决赛选手正常出现（新 m 胜者 1，m2 客胜 2）
    client.post(f"/api/matches/{m2}/score", json={"score1": 1, "score2": 3})
    st = state(client)
    assert st["finalists"]["ready"] is True
    assert {w["player"]["id"] for w in st["finalists"]["winners"]} == {1, 2}


def test_generic_pairing_of_same_two_also_starts_blank(client):
    submit_tie_group_scores(client)
    st = state(client)
    m1 = st["tables"]["1"]["id"]
    client.post(f"/api/matches/{m1}/score", json={"score1": 3, "score2": 1})
    client.post(f"/api/matches/{m1}/withdraw")

    # 通过通用配对接口让“以后再配到一起”
    r = client.post("/pairings",
                    json={"player1_id": 1, "player2_id": 5, "table_no": 1})
    assert r.status_code == 200
    body = r.get_json()
    assert body["match"]["score1"] is None
    assert body["match"]["status"] == "active"

    # 旧 id 依旧拒绝交分
    assert client.post(f"/api/matches/{m1}/score",
                       json={"score1": 9, "score2": 9}).status_code == 409


# --------------------------------------------------------------------------- #
# 小组对阵更新对已有半决赛记录的影响
# --------------------------------------------------------------------------- #

def test_group_resubmit_with_changed_lineup_replaces_blank_provisional(client):
    # 初始临时对阵：1-5 / 4-2
    # 改一组完全不同的结果，让 A 组 p3 成为 A1
    data = {
        "score_1_p1": "0", "score_1_p2": "3",  # 2 胜 1
        "score_2_p1": "0", "score_2_p2": "3",  # 3 胜 1
        "score_3_p1": "1", "score_3_p2": "3",  # 3 胜 2 => p3 两胜 A1
        "score_4_p1": "3", "score_4_p2": "0",
        "score_5_p1": "3", "score_5_p2": "0",
        "score_6_p1": "0", "score_6_p2": "3",  # 6 胜 5 => B: 4,6,5? 4两胜
    }
    assert client.post("/groups/scores", data=data).status_code == 302
    st = state(client)
    pairs = {tuple(sorted((m["player1_id"], m["player2_id"])))
             for m in st["active_semifinals"]}
    # A1=3, A2=2 ; B1=4(2胜), B2=6 或? B: 4 wins2; 6: beat5 =>1; 5:0?
    # m4 4 beats5, m5 4 beats6, m6 6 beats5 -> 6 one win,5 zero => B2=6
    assert pairs == {tuple(sorted((3, 6))), tuple(sorted((4, 2)))}


def test_scored_semifinal_is_archived_when_lineup_changes(client):
    submit_tie_group_scores(client)
    m1 = state(client)["tables"]["1"]["id"]
    client.post(f"/api/matches/{m1}/score", json={"score1": 3, "score2": 1})

    # 相同排名再次整表提交（更正小组比分但赢家不变）-> 半决赛比分保留
    again = submit_tie_group_scores(client)
    assert again.status_code == 302
    m = next(x for x in state(client)["matches"] if x["id"] == m1)
    assert m["status"] == "active" and (m["score1"], m["score2"]) == (3, 1)

    # 颠覆 A 组排名 -> 旧 m1 对阵不再需要：有比分 => 封存可查，新场空白
    data = {
        "score_1_p1": "0", "score_1_p2": "3",
        "score_2_p1": "0", "score_2_p2": "3",
        "score_3_p1": "1", "score_3_p2": "3",
        "score_4_p1": "3", "score_4_p2": "1",
        "score_5_p1": "3", "score_5_p2": "0",
        "score_6_p1": "3", "score_6_p2": "1",
    }
    client.post("/groups/scores", data=data)
    m = next(x for x in state(client)["matches"] if x["id"] == m1)
    assert m["status"] == "withdrawn"
    assert m["withdraw_reason"] == "lineup_update"
    assert (m["score1"], m["score2"]) == (3, 1)
    assert client.post(f"/api/matches/{m1}/score",
                       json={"score1": 1, "score2": 3}).status_code == 409


def test_swap_requires_two_active_semifinals(client):
    submit_tie_group_scores(client)
    m1 = state(client)["tables"]["1"]["id"]
    client.post(f"/matches/{m1}/withdraw")
    r = client.post("/api/semifinals/swap-tables")
    assert r.status_code == 409
    assert r.get_json()["error"] == "need_two_semifinals"


def test_table_endpoint_resolves_by_pair_after_swap(client):
    submit_tie_group_scores(client)
    st = state(client)
    m1 = st["tables"]["1"]["id"]
    client.post("/semifinals/swap-tables")  # m1 -> 2 号台

    # 不带 match_id，只带两名球员，旧一号台页面也能找到已换到二号台的场次
    r = client.post("/tables/1/score",
                    data={"player1_id": "1", "player2_id": "5",
                          "score1": "3", "score2": "2"})
    assert r.status_code == 302
    m = next(x for x in state(client)["matches"] if x["id"] == m1)
    assert (m["score1"], m["score2"], m["table_no"]) == (3, 2, 2)

    # 语义化别名同样可用
    r = client.post(f"/semifinals/{m1}/score",
                    json={"score1": 3, "score2": 1})
    assert r.status_code == 200


def test_healthz(client):
    assert client.get("/healthz").get_json() == {"ok": True}


# --------------------------------------------------------------------------- #
# 重复排人：重配拦截并指出冲突场次
# --------------------------------------------------------------------------- #

def test_repair_rejects_player_already_in_another_active_sf_json(client):
    st = state(client)
    m1, m2 = st["tables"]["1"]["id"], st["tables"]["2"]["id"]
    assert client.post(f"/matches/{m2}/withdraw").status_code == 302

    # 球员 1 仍在 m1（一号台），把他和 3 号排到空出的二号台 -> 409
    r = client.post("/pairings",
                    json={"player1_id": 1, "player2_id": 3, "table_no": 2})
    assert r.status_code == 409
    body = r.get_json()
    assert body["code"] == "sf_player_conflict"
    assert body["conflict_match_ids"] == [m1]
    assert f"#{m1}" in body["message"]

    # 数据库里没有多出任何 active SF
    st = state(client)
    assert [m["id"] for m in st["active_semifinals"]] == [m1]
    assert st["sf_conflicts"]["has_conflict"] is False


def test_repair_form_redirects_to_conflict_match_with_flash(client):
    st = state(client)
    m2 = st["tables"]["2"]["id"]
    m1 = st["tables"]["1"]["id"]
    client.post(f"/matches/{m2}/withdraw")

    resp = client.post("/pairings",
                       data={"player1_id": "6", "player2_id": "1",
                             "table_no": "2"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/#match-{m1}")

    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    # 跟随锚点取首页：flash 文案指出冲突场次编号
    assert f"#{m1}" in page and "不能同时被排进两场半决赛" in page


def test_repair_from_withdrawn_also_blocks_overlap(client):
    st = state(client)
    m1, m2 = st["tables"]["1"]["id"], st["tables"]["2"]["id"]
    # 撤下 m2 后，尝试用 m2 的“原班两人重配”不会撞人（4-2 都空闲）-> 成功
    client.post(f"/matches/{m2}/withdraw")
    ok = client.post(f"/matches/{m2}/re-pair", data={"table_no": "2"})
    assert ok.status_code == 302

    # 再撤下 m2 的新场次；直接构造“和仍在 m1 的球员 1 配对”的通用请求被拒
    m2new = state(client)["tables"]["2"]["id"]
    client.post(f"/matches/{m2new}/withdraw")
    r = client.post("/pairings",
                    json={"player1_id": 2, "player2_id": 1, "table_no": 2})
    assert r.status_code == 409
    assert r.get_json()["conflict_match_ids"] == [m1]


# --------------------------------------------------------------------------- #
# 已有重复赛程（历史脏数据）：网页标注、比分可查、冻结晋级/决赛/冠军、恢复
# --------------------------------------------------------------------------- #

def inject_legacy_conflict(db_path):
    """模拟旧版本 bug 产生的脏赛程（含据冲突赛程产生并已记分的决赛）。

    #1 SF 1-5 台1  3:1（胜者 1）
    #2 SF 4-2 台2  withdrawn
    #3 SF 1-3 台2  1:3（胜者 3）  -> 1 重复排人；两场胜者不同(1,3)
    #4 F  1-3      active 4:3     -> 据冲突赛程产生的决赛与比分
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE matches SET status='withdrawn' WHERE id=2")
        conn.execute("UPDATE matches SET score1=3, score2=1 WHERE id=1")
        conn.execute(
            "INSERT INTO matches (id, round, bracket_code, table_no,"
            " player1_id, player2_id, status, pair_key, created_at,"
            " score1, score2) VALUES"
            " (3,'SF','REPAIR',2,1,3,'active','1-3','t',1,3)")
        conn.execute(
            "INSERT INTO matches (id, round, bracket_code, table_no,"
            " player1_id, player2_id, status, pair_key, created_at,"
            " score1, score2) VALUES"
            " (4,'F','F1',NULL,1,3,'active','1-3','t',4,3)")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def dirty_app(app):
    inject_legacy_conflict(app.config["DB_PATH"])
    return app


@pytest.fixture()
def dirty_client(dirty_app):
    return dirty_app.test_client()


def test_existing_duplicates_are_flagged_and_scores_stay_visible(dirty_client):
    st = dirty_client.get("/api/state").get_json()
    rep = st["sf_conflicts"]
    assert rep["has_conflict"] is True
    assert rep["player_ids"] == [1]
    assert rep["match_ids"] == [1, 3]

    home = dirty_client.get("/").get_data(as_text=True)
    assert 'data-testid="sf-conflict-banner"' in home
    assert 'data-testid="conflict-player-1"' in home
    assert 'data-testid="conflict-match-1"' in home
    assert 'data-testid="conflict-match-3"' in home
    assert 'data-testid="conflict-badge-3"' in home
    # 两场半决赛的比分仍然渲染可查
    assert 'data-testid="score-1-1">3<' in home
    assert 'data-testid="score-3-2">3<' in home

    hist = dirty_client.get("/history").get_data(as_text=True)
    assert 'data-testid="history-conflict-banner"' in hist
    assert 'data-testid="history-conflict-match-3"' in hist
    assert 'data-conflict="1"' in hist
    assert "1 : 3" in hist and "3 : 1" in hist


def test_conflict_suspends_advancement_final_scoring_and_champion(dirty_client):
    st = dirty_client.get("/api/state").get_json()
    fs = st["finalists"]
    assert fs["ready"] is False and fs["conflict"] is True
    # 即使两场胜者不同（1 与 3），也不给出胜者
    assert fs["winners"] == []
    assert st["champion"] is None

    home = dirty_client.get("/").get_data(as_text=True)
    assert 'data-testid="final-suspended"' in home
    assert 'data-testid="finalist-names"' not in home
    assert 'data-testid="champion"' not in home

    # 直接给据冲突赛程产生的决赛 #4 交分 -> 409 semifinal_conflict
    r = dirty_client.post("/api/matches/4/score",
                          json={"score1": 5, "score2": 4})
    assert r.status_code == 409
    assert r.get_json()["code"] == "semifinal_conflict"

    # 该决赛被封存，尝试的新比分未写入，原 4:3 保留
    st = dirty_client.get("/api/state").get_json()
    f4 = next(m for m in st["matches"] if m["id"] == 4)
    assert f4["status"] == "withdrawn"
    assert f4["withdraw_reason"] == "sf_conflict"
    assert (f4["score1"], f4["score2"]) == (4, 3)

    # 决赛独立页：冲突期间 GET 不提供记分表单，改为暂停面板
    page = dirty_client.get("/matches/4").get_data(as_text=True)
    assert "sealed-box" in page  # 已被上面的 POST 封存


def test_final_page_blocks_form_while_active_final_and_conflict(app):
    inject_legacy_conflict(app.config["DB_PATH"])
    client = app.test_client()
    page = client.get("/matches/4").get_data(as_text=True)
    assert 'data-testid="final-score-blocked"' in page
    assert 'data-testid="score-form-4"' not in page

    resp = client.post("/matches/4/score", data={"score1": "9", "score2": "8"})
    assert resp.status_code == 409
    html = resp.get_data(as_text=True)
    assert 'data-testid="conflict-block-warning"' in html
    assert "#1" in html and "#3" in html


def test_withdraw_and_compliant_results_rebuild_final_and_archive_old_one(
        dirty_client):
    # 撤下重复场 #3
    assert dirty_client.post("/api/matches/3/withdraw").status_code == 200

    # 补齐合规的第二场半决赛 4 对 2（二号台）
    r = dirty_client.post("/pairings",
                          json={"player1_id": 4, "player2_id": 2,
                                "table_no": 2})
    assert r.status_code == 200
    new_sf = r.get_json()["new_match_id"]

    # #1 已有 3:1（胜者 1）；新场 2:3 -> 球员 2 胜
    r = dirty_client.post(f"/api/matches/{new_sf}/score",
                          json={"score1": 2, "score2": 3})
    assert r.status_code == 200
    assert r.get_json()["finalists"]["ready"] is True

    st = dirty_client.get("/api/state").get_json()
    assert st["sf_conflicts"]["has_conflict"] is False
    assert {w["player"]["id"] for w in st["finalists"]["winners"]} == {1, 2}

    # 按有效胜者生成 1-2 的空白新决赛
    fnew = st["final_match"]
    assert fnew["status"] == "active"
    assert {fnew["player1_id"], fnew["player2_id"]} == {1, 2}
    assert fnew["score1"] is None and fnew["score2"] is None

    # 旧的冲突决赛 #4 封存、原比分 4:3 保留可查
    f4 = next(m for m in st["matches"] if m["id"] == 4)
    assert f4["status"] == "withdrawn"
    assert (f4["score1"], f4["score2"]) == (4, 3)
    hist = dirty_client.get("/history").get_data(as_text=True)
    assert "4 : 3" in hist and "半决赛重复排人冲突" in hist

    # 新决赛记分后冠军恢复
    r = dirty_client.post(f"/api/matches/{fnew['id']}/score",
                          json={"score1": 3, "score2": 2})
    assert r.status_code == 200
    st = dirty_client.get("/api/state").get_json()
    assert st["champion"]["player"]["id"] == 1
    home = dirty_client.get("/").get_data(as_text=True)
    assert 'data-testid="champion"' in home and "陈一鸣" in home


# --------------------------------------------------------------------------- #
# 并发重配：两个裁判同时给两张空台排同一人，只有一份成功
# --------------------------------------------------------------------------- #

def test_dirty_schedule_survives_restart_and_final_gets_sealed(app):
    db_path = app.config["DB_PATH"]
    inject_legacy_conflict(db_path)

    # 模拟容器/进程重启：重新 create_app 会跑启动调和
    app2 = create_app({"TESTING": True, "DB_PATH": db_path})
    client2 = app2.test_client()
    st = client2.get("/api/state").get_json()

    # 冲突赛程没有被启动调和抹掉，网页仍可标注
    assert st["sf_conflicts"]["has_conflict"] is True
    assert st["sf_conflicts"]["match_ids"] == [1, 3]
    # 据冲突赛程产生的有比分决赛在启动调和时以 sf_conflict 封存
    f4 = next(m for m in st["matches"] if m["id"] == 4)
    assert f4["status"] == "withdrawn"
    assert f4["withdraw_reason"] == "sf_conflict"
    assert (f4["score1"], f4["score2"]) == (4, 3)
    assert st["champion"] is None


def test_concurrent_pairing_same_player_two_empty_tables_only_one_wins(app):
    db_path = app.config["DB_PATH"]
    client = app.test_client()
    st = state(client)
    # 两张台全部撤空
    for m in st["active_semifinals"]:
        assert client.post(f"/api/matches/{m['id']}/withdraw").status_code == 200
    assert state(client)["active_semifinals"] == []

    barrier = threading.Barrier(2)
    results: list[tuple[str, int | None, str | None]] = []

    def worker(table, partner):
        conn = connect(str(db_path))
        try:
            barrier.wait(timeout=10)
            try:
                new_id = create_repair(
                    conn, player1_id=1, player2_id=partner, table_no=table)
                results.append(("ok", new_id, None))
            except PlayerAlreadyScheduledError as exc:
                results.append(("conflict", None, str(exc.conflict_match_ids)))
            finally:
                conn.close()
        except threading.BrokenBarrierError:
            results.append(("barrier", None, None))

    t1 = threading.Thread(target=worker, args=(1, 3))
    t2 = threading.Thread(target=worker, args=(2, 6))
    t1.start(); t2.start(); t1.join(timeout=20); t2.join(timeout=20)

    assert [r[0] for r in results].count("ok") == 1, results
    assert [r[0] for r in results].count("conflict") == 1, results

    # 不变量：球员 1 只在一场 active SF；系统无冲突标记
    st = state(client)
    involving_1 = [
        m for m in st["active_semifinals"]
        if 1 in (m["player1_id"], m["player2_id"])
    ]
    assert len(involving_1) == 1
    assert st["sf_conflicts"]["has_conflict"] is False


def test_score_and_table_on_missing_or_withdrawn_match_do_not_500(client):
    # 不存在的场次：HTML 交分 -> 404 页；JSON -> 409 既有契约
    assert client.post("/matches/99999/score",
                       data={"score1": "1", "score2": "0"}).status_code == 404
    js = client.post("/api/matches/99999/score", json={"score1": 1, "score2": 0})
    assert js.status_code == 409
    assert js.get_json()["code"] == "match_withdrawn"

    # 已撤下场次走换台表单：409 渲染提示页而不是 500
    m2 = state(client)["tables"]["2"]["id"]
    assert client.post(f"/matches/{m2}/withdraw").status_code == 302
    resp = client.post(f"/matches/{m2}/table", data={"table_no": "1"})
    assert resp.status_code == 409
    assert "stale-warning" in resp.get_data(as_text=True)
    assert client.post("/matches/99999/table",
                       data={"table_no": "1"}).status_code == 302
