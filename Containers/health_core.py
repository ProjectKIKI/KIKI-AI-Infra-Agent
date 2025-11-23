#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
health_core.py

SQLite 기반 메트릭/로그 저장/조회 공통 로직.
kiki의 health-collect / health-ai / log-ai에서 import 해서 사용.
"""

import sqlite3
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def init_db(db_path: str) -> None:
    """metrics/logs 테이블 생성 (존재하면 무시)."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        # 메트릭 테이블
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                host TEXT NOT NULL,
                source TEXT NOT NULL,
                cpu_load1 REAL,
                cpu_load5 REAL,
                cpu_load15 REAL,
                mem_used_mb REAL,
                mem_total_mb REAL,
                mem_used_pct REAL,
                disk_root_used_pct REAL,
                disk_root_used_gb REAL,
                disk_root_total_gb REAL,
                error_count INTEGER,
                warn_count INTEGER,
                extra_json TEXT
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_metrics_ts_host ON metrics(ts, host)"
        )

        # (옵션) 로그 테이블 - 필요하면 사용
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                host TEXT NOT NULL,
                source TEXT NOT NULL,
                level TEXT,
                service TEXT,
                message TEXT
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_logs_ts_host ON logs(ts, host)"
        )

        conn.commit()
    finally:
        conn.close()


def insert_metric(
    db_path: str,
    host: str,
    source: str,
    metrics: Dict[str, Any],
    ts: Optional[int] = None,
) -> None:
    """kiki_metrics dict를 받아 metrics 테이블에 한 줄 저장."""
    ts_val = int(ts or time.time())
    extra = metrics.get("extra") or {}

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO metrics (
                ts, host, source,
                cpu_load1, cpu_load5, cpu_load15,
                mem_used_mb, mem_total_mb, mem_used_pct,
                disk_root_used_pct, disk_root_used_gb, disk_root_total_gb,
                error_count, warn_count,
                extra_json
            ) VALUES (?, ?, ?,
                      ?, ?, ?,
                      ?, ?, ?,
                      ?, ?, ?,
                      ?, ?,
                      ?)
            """,
            (
                ts_val,
                host,
                source,
                metrics.get("cpu_load1"),
                metrics.get("cpu_load5"),
                metrics.get("cpu_load15"),
                metrics.get("mem_used_mb"),
                metrics.get("mem_total_mb"),
                metrics.get("mem_used_pct"),
                metrics.get("disk_root_used_pct"),
                metrics.get("disk_root_used_gb"),
                metrics.get("disk_root_total_gb"),
                metrics.get("error_count"),
                metrics.get("warn_count"),
                json.dumps(extra, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def query_metrics_since(
    db_path: str,
    since_sec: int,
    hosts: Optional[List[str]] = None,
    source: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """최근 since_sec 초 동안의 metrics를 조회해서 dict 리스트로 반환."""
    ts_min = int(time.time()) - since_sec
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        query = "SELECT * FROM metrics WHERE ts >= ?"
        args: List[Any] = [ts_min]

        if hosts:
            placeholders = ",".join("?" for _ in hosts)
            query += f" AND host IN ({placeholders})"
            args.extend(hosts)

        if source:
            query += " AND source = ?"
            args.append(source)

        query += " ORDER BY ts ASC"
        cur.execute(query, args)
        rows = cur.fetchall()
        result: List[Dict[str, Any]] = []
        for r in rows:
            row_dict = dict(r)
            if row_dict.get("extra_json"):
                try:
                    row_dict["extra"] = json.loads(row_dict["extra_json"])
                except Exception:
                    row_dict["extra"] = None
            result.append(row_dict)
        return result
    finally:
        conn.close()


def tail_file(path: str, max_lines: int = 2000) -> str:
    """큰 로그 파일에서 뒤에서 max_lines 줄만 읽어 반환."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))

    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)
