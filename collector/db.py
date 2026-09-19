"""SQLite 스키마와 연결.

왜 Postgres 가 아닌가
---------------------
본체는 Postgres 를 쓴다(`app/database/postgres.py`). 그런데 수집기는 **스택이 안 떠도
돌아야** 하는 것이 존재 이유다(collector/config.py 머리말). SQLite 는 파일 하나이고
파이썬에 들어 있어 그 조건을 그대로 만족한다.

옮겨 갈 때를 대비해 표 모양은 Postgres 로 그대로 번역되게 잡아 두었다 — 특수 타입을
쓰지 않고, 기본키를 자연키로 잡았다. 옮기는 일은 적재가 안정된 뒤에 한다.

표 셋
-----
``raw_response``   응답 원문. 정규화를 다시 돌릴 수 있는 **유일한** 근거.
``price_daily``    정규화한 일별 시세. 분석이 읽는 표.
``ingest_day``     기준일별 수집 상태. 중단한 자리에서 다시 시작하는 근거.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from collector import config

SCHEMA = """
-- ── 1. 응답 원문 ────────────────────────────────────────────────────────────
-- 압축 전 원문의 sha256 을 함께 둔다. 압축 결과는 라이브러리 버전에 따라 달라질 수
-- 있어서, 그 값을 지문으로 삼으면 언젠가 "같은 자료인데 다르다" 가 나온다.
CREATE TABLE IF NOT EXISTS raw_response (
    source      TEXT    NOT NULL,          -- portal | kis | ecos | dart
    target      TEXT    NOT NULL,          -- 예: price/20260917
    fetched_at  TEXT    NOT NULL,          -- KST ISO8601. "언제부터 알 수 있었나"의 근거
    body        BLOB    NOT NULL,          -- gzip 압축한 응답 바이트
    sha256      TEXT    NOT NULL,          -- 압축 '전' 원문의 지문
    bytes       INTEGER NOT NULL,          -- 압축 전 크기
    compression TEXT    NOT NULL DEFAULT 'gzip',
    http_status INTEGER,
    note        TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (source, target, fetched_at)
);
CREATE INDEX IF NOT EXISTS ix_raw_source_target ON raw_response(source, target);

-- ── 2. 정규화한 일별 시세 ───────────────────────────────────────────────────
-- 값은 포털 응답 그대로다. 수정주가는 여기에 **담지 않는다** — 조정은 과거 전체를
-- 다시 쓰는 일이라, 원본 표를 덮어쓰면 "원본이 무엇이었나" 를 잃는다.
-- 조정은 preprocess 가 읽어서 파생 표로 만든다.
CREATE TABLE IF NOT EXISTS price_daily (
    bas_dt      TEXT    NOT NULL,          -- YYYYMMDD
    srtn_cd     TEXT    NOT NULL,          -- 단축코드 6자리
    isin_cd     TEXT    NOT NULL DEFAULT '',
    itms_nm     TEXT    NOT NULL DEFAULT '',
    mrkt_ctg    TEXT    NOT NULL DEFAULT '',   -- KOSPI | KOSDAQ | KONEX
    clpr        INTEGER,                   -- 종가
    vs          INTEGER,                   -- 전일 대비 '금액'. 조정계수의 근거(README 참고)
    flt_rt      REAL,                      -- 전일 대비 '등락률'. 소수 2자리라 검증용으로만
    mkp         INTEGER,                   -- 시가. 0 이면 그날 거래가 없었다
    hipr        INTEGER,
    lopr        INTEGER,
    trqu        INTEGER,                   -- 거래량
    tr_prc      INTEGER,                   -- 거래대금
    lstg_st_cnt INTEGER,                   -- 상장주식수. 분할·증자를 여기서도 볼 수 있다
    mrkt_tot_amt INTEGER,                  -- 시가총액
    halted      INTEGER NOT NULL DEFAULT 0,-- 거래정지 판정(0/1). 판정 규칙은 preprocess
    raw_sha256  TEXT    NOT NULL DEFAULT '', -- 이 행이 나온 원문
    PRIMARY KEY (bas_dt, srtn_cd)
);
CREATE INDEX IF NOT EXISTS ix_price_srtn ON price_daily(srtn_cd, bas_dt);
CREATE INDEX IF NOT EXISTS ix_price_mrkt ON price_daily(mrkt_ctg, bas_dt);

-- ── 3. 기준일별 수집 상태 ───────────────────────────────────────────────────
-- 백필은 몇 시간 걸린다. 중간에 끊긴다는 전제로 만든다.
--
-- status 가 넷인 이유:
--   done     행을 받아 넣었다
--   empty    응답은 정상인데 0건이었다. **휴장일과 "아직 안 올라온 거래일" 이
--            똑같이 0건으로 온다** — 포털은 둘을 구분해 주지 않는다(실측 2026-09-19).
--            그래서 empty 는 확정이 아니라 보류다.
--   holiday  empty 가 재시도 한도를 넘겨 휴장으로 확정한 것
--   error    호출 자체가 실패했다. 재시도 대상
CREATE TABLE IF NOT EXISTS ingest_day (
    source      TEXT    NOT NULL,
    bas_dt      TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    rows        INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    message     TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (source, bas_dt)
);
CREATE INDEX IF NOT EXISTS ix_ingest_status ON ingest_day(source, status);

-- ── 4. 수정주가 (파생) ──────────────────────────────────────────────────────
-- price_daily 를 덮어쓰지 않고 따로 쌓는다. 조정은 규칙이 바뀌면 전부 다시 계산해야
-- 하는데, 원본 표에 덮어쓰면 "원본이 무엇이었나" 를 잃어 되돌릴 수 없다.
-- 이 표는 언제든 통째로 지우고 preprocess 가 다시 만든다.
CREATE TABLE IF NOT EXISTS price_adjusted (
    bas_dt      TEXT    NOT NULL,
    srtn_cd     TEXT    NOT NULL,
    adj_clpr    REAL,                      -- 조정 종가 (가장 최근 날을 1.0 기준으로)
    adj_mkp     REAL,
    adj_hipr    REAL,
    adj_lopr    REAL,
    cum_factor  REAL    NOT NULL,          -- 이 날에 곱해진 누적 조정계수
    PRIMARY KEY (bas_dt, srtn_cd)
);
CREATE INDEX IF NOT EXISTS ix_adj_srtn ON price_adjusted(srtn_cd, bas_dt);

-- ── 5. 조정 이벤트 ──────────────────────────────────────────────────────────
-- 분할·병합·권리락처럼 '가격이 끊기는' 날. 사람이 눈으로 확인할 수 있게 남긴다.
-- 포털은 조정 사유를 알려 주지 않으므로(KIS 도 분할일에 사유 필드가 비어 있었다, #33)
-- **사유 칸은 비워 두고 계수만 적는다.** 추측해서 채우지 않는다.
CREATE TABLE IF NOT EXISTS corporate_action (
    bas_dt      TEXT    NOT NULL,
    srtn_cd     TEXT    NOT NULL,
    itms_nm     TEXT    NOT NULL DEFAULT '',
    prev_clpr   INTEGER,                   -- 직전 거래일 종가 (조정 전)
    base_price  INTEGER,                   -- clpr - vs. 거래소가 정한 '조정 기준가'
    factor      REAL    NOT NULL,          -- base_price / prev_clpr
    lstg_before INTEGER,                   -- 상장주식수 변화도 함께 본다 (교차 확인)
    lstg_after  INTEGER,
    -- f(가격계수) x q(주식수비율). 분할이면 1 이어야 한다. 1 에서 멀면 두 신호가
    -- 어긋난 것이고, 어느 쪽이 맞는지는 자동으로 정하지 않는다 (preprocess 머리말).
    lstg_cross  REAL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    -- split=주식수 변화와 일치 / rights=권리락(주식수 불변) / review=두 신호 모두 어긋남
    kind        TEXT    NOT NULL DEFAULT 'review',
    PRIMARY KEY (bas_dt, srtn_cd)
);
"""


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """연결을 열고 표를 보장한다.

    ``isolation_level=None`` 은 파이썬이 몰래 트랜잭션을 여는 것을 끄는 설정이다.
    트랜잭션 경계를 호출하는 쪽이 `BEGIN IMMEDIATE` 로 직접 잡는다 — 원문·정규화·
    상태가 **함께** 커밋돼야 하기 때문이다. 하나만 남으면 재개 로직이 거짓말을 한다.
    """
    config.ensure_dirs()
    conn = sqlite3.connect(db_path or config.DB_PATH, timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    # WAL: 수집이 도는 중에도 다른 프로세스가 읽을 수 있다. 대시보드가 이 경로를 탄다.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn
