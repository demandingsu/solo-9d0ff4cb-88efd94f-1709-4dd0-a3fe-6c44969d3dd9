#!/usr/bin/env python3
"""黑盒并发试跑：两个裁判同时给两张空台排同一名球员，只能有一份成功。

仅用标准库（线程 + urllib），对真实 gunicorn 多 worker 运行最有说服力：

    python3 scripts/e2e_concurrent_repair.py http://127.0.0.1:5566 --rounds 8

每轮先通过 HTTP 撤下全部现行半决赛（两张台全空），然后用 Barrier 让两个
请求尽量同时发出：
    裁判 A：把 1 号球员与 3 号排到一号台
    裁判 B：把 1 号球员与 6 号排到二号台
断言：恰好一个 200，另一个 409/sf_player_conflict；服务端始终满足
“一名球员至多在一场现行半决赛”。另含一组“完全相同配对并发提交”的对照。
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--")
        else "http://127.0.0.1:5000").rstrip("/")
ROUNDS = 8
if "--rounds" in sys.argv:
    ROUNDS = int(sys.argv[sys.argv.index("--rounds") + 1])

failures: list[str] = []
passed = 0


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failures.append(f"{name} {detail}")
        print(f"  ❌ {name} {detail}")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(
        BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def get_state():
    status, text = call("GET", "/api/state")
    assert status == 200, (status, text[:200])
    return json.loads(text)


def empty_all_tables():
    st = get_state()
    for m in st["active_semifinals"]:
        status, _ = call("POST", f"/api/matches/{m['id']}/withdraw", body={})
        assert status == 200, status
    st = get_state()
    assert st["active_semifinals"] == [], st["active_semifinals"]


def concurrent_posts(specs):
    """specs: [(path, body), ...] 同时发出，返回 [(status, json_or_none)]。"""
    barrier = threading.Barrier(len(specs))
    outcomes: list[tuple[int, dict | None]] = [None] * len(specs)  # type: ignore

    def runner(idx, path, body):
        barrier.wait(timeout=10)
        status, text = call("POST", path, body=body)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        outcomes[idx] = (status, parsed)

    threads = [
        threading.Thread(target=runner, args=(i, p, b))
        for i, (p, b) in enumerate(specs)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return outcomes


def assert_invariant(round_label):
    st = get_state()
    usage: dict[int, list[int]] = {}
    for m in st["active_semifinals"]:
        for pid in {m["player1_id"], m["player2_id"]}:
            usage.setdefault(pid, []).append(m["id"])
    duplicated = {p: ids for p, ids in usage.items() if len(set(ids)) > 1}
    check(f"[{round_label}] 不变量：没有球员被排进两场现行半决赛",
          not duplicated, str(duplicated))
    check(f"[{round_label}] 状态接口无冲突标记",
          st["sf_conflicts"]["has_conflict"] is False)


def main():
    print(f"▶ 对 {BASE} 进行并发重配试跑（{ROUNDS} 轮同人两空台 + 1 轮同配对）")
    status, _ = call("GET", "/healthz")
    check("健康检查 200", status == 200, str(status))

    winner_ids: list[int] = []
    for rnd in range(1, ROUNDS + 1):
        empty_all_tables()
        outcomes = concurrent_posts([
            ("/pairings", {"player1_id": 1, "player2_id": 3, "table_no": 1}),
            ("/pairings", {"player1_id": 1, "player2_id": 6, "table_no": 2}),
        ])
        s_a, j_a = outcomes[0]
        s_b, j_b = outcomes[1]
        codes = [
            (s, (j or {}).get("code") or (j or {}).get("error"))
            for s, j in outcomes
        ]
        oks = [(s, j) for s, j in outcomes if s == 200]
        conflicts = [
            (s, j) for s, j in outcomes
            if s == 409 and (j or {}).get("code") == "sf_player_conflict"
        ]
        check(f"第 {rnd} 轮：恰好 1 份成功（实际 {len(oks)}），另 1 份 "
              f"409/sf_player_conflict（实际 {len(conflicts)}）",
              len(oks) == 1 and len(conflicts) == 1, str(codes))
        if oks:
            winner_ids.append(oks[0][1].get("new_match_id"))
        if conflicts:
            j = conflicts[0][1]
            check(f"第 {rnd} 轮：落败响应指出了获胜方场次",
                  bool(j.get("conflict_match_ids"))
                  and f"#{j['conflict_match_ids'][0]}" in j.get("message", ""),
                  str(j)[:220])
        assert_invariant(f"第 {rnd} 轮后")

    # 对照：两张空台 + 完全相同的配对/球台同时提交，也只能一份成功
    empty_all_tables()
    outcomes = concurrent_posts([
        ("/pairings", {"player1_id": 2, "player2_id": 5, "table_no": 1}),
        ("/pairings", {"player1_id": 2, "player2_id": 5, "table_no": 1}),
    ])
    oks = [o for o in outcomes if o[0] == 200]
    rejects = [o for o in outcomes if o[0] == 409]
    check("相同配对并发：恰好 1 份成功，另 1 份 409（重复配对/冲突）",
          len(oks) == 1 and len(rejects) == 1,
          str([(s, (j or {}).get("code")) for s, j in outcomes]))
    assert_invariant("相同配对轮后")

    print()
    if failures:
        print(f"❌ {len(failures)} 项失败 / {passed} 项通过")
        for f in failures:
            print("  -", f)
        return 1
    print(f"🎉 全部 {passed} 项并发检查通过；成功场次 id 序列 {winner_ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
