#!/usr/bin/env python3
"""注入“旧版本 bug”会产生的重复排人脏赛程（仅用于试跑/演示，会清空淘汰赛表）。

本地：
    DB_PATH=/tmp/trial_referee.db python3 scripts/dev_inject_conflict.py
容器（Docker Compose）：
    docker compose cp scripts/dev_inject_conflict.py referee:/tmp/inject.py
    docker compose exec referee python3 /tmp/inject.py

注入后的固定场景（球员 id 由播种数据确定）：
    #1 SF 1 陈一鸣 vs 5 吴俊杰  一号台 active，比分 3:1（胜者 1）
    #2 SF 4 周雨桐 vs 2 林浩然  二号台 withdrawn
    #3 SF 1 陈一鸣 vs 3 赵子轩  二号台 active，比分 1:3（胜者 3）
       -> 球员 1 同时在 #1/#3；两场胜者不同（1 与 3）
    #4 F  1 陈一鸣 vs 3 赵子轩  active，比分 4:3
       -> 据冲突赛程产生、已记分的决赛（用于验证冻结与恢复后封存）
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = "/data/referee.db"

SQL = """
DELETE FROM matches;
DELETE FROM sqlite_sequence WHERE name = 'matches';

INSERT INTO matches
  (id, round, bracket_code, table_no, player1_id, player2_id,
   status, pair_key, created_at, updated_at, withdrawn_at,
   withdraw_reason, score1, score2)
VALUES
  (1, 'SF', 'SF1',    1, 1, 5, 'active',    '1-5',
   '2026-01-01T00:00:00+00:00', NULL, NULL, NULL, 3, 1),
  (2, 'SF', 'SF2',    2, 4, 2, 'withdrawn', '2-4',
   '2026-01-01T00:00:00+00:00', NULL, '2026-01-02T00:00:00+00:00',
   'manual', NULL, NULL),
  (3, 'SF', 'REPAIR', 2, 1, 3, 'active',    '1-3',
   '2026-01-03T00:00:00+00:00', NULL, NULL, NULL, 1, 3),
  (4, 'F',  'F1',  NULL, 1, 3, 'active',    '1-3',
   '2026-01-04T00:00:00+00:00', NULL, NULL, NULL, 4, 3);
"""


def db_path_from_argv_env() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    return os.environ.get("DB_PATH", DEFAULT_DB)


def inject(db_path: str) -> None:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.executescript(SQL)
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM matches WHERE status='active'"
        ).fetchone()[0]
        print(f"[inject] {db_path}: 4 条淘汰赛记录已就位，其中 active {n} 条；"
              "冲突 = 球员 1 同时在 #1、#3，决赛 #4 = 1 vs 3 (4:3)")
    finally:
        conn.close()


if __name__ == "__main__":
    inject(db_path_from_argv_env())
