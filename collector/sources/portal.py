"""공공데이터포털 — 금융위원회 주식시세정보 (getStockPriceInfo).

이 파일이 백필의 심장이다. 1,648 거래일을 여기로 받는다.

왜 기간 조회가 아니라 하루씩인가
--------------------------------
API 는 ``beginBasDt``/``endBasDt`` 로 기간 조회를 받는다. 그런데 **``endBasDt`` 가
배타적이다** — 끝 날짜가 결과에 들어오지 않는다(#33, 실측). 경계에서 하루가 조용히
빠지는 종류의 버그는 나중에 성과 수치로만 드러나고, 그때는 원인을 못 찾는다.

게다가 기간 조회는 한 응답에 여러 날이 섞여 들어와, ``numOfRows`` 상한(한 번에 받을 수
있는 행 수)에 언제 걸릴지가 종목 수에 따라 달라진다. **하루치 전종목**은 크기가 예측
가능하다 — 실측 2,870행 · 784 KB 로, 한 번에 안전하게 들어온다.

하루 한 번 부르면 1,648 호출이고 하루 한도 10,000 의 17% 다. 하루에 끝난다.

휴장일과 "아직 안 올라온 날"이 구분되지 않는다
----------------------------------------------
실측 2026-09-19 11:00 KST::

    basDt=20260917(목)  totalCount=2870   ← 있다
    basDt=20260918(금)  totalCount=0      ← 거래일인데 없다 (아직 안 올라옴)
    basDt=20260913(일)  totalCount=0      ← 휴장일
    basDt=20260912(토)  totalCount=0      ← 휴장일

둘 다 ``resultCode=00`` 에 ``totalCount=0`` 으로 **똑같이** 온다. 포털은 둘을 구분해
주지 않는다.

여기서 나오는 결론이 설계를 바꾼다: **"오늘 장 끝나고 오늘 것을 받는" 일정은 성립하지
않는다.** 금요일 시세가 토요일 오전에도 아직 없었다. 그래서 일일 수집은 당일을 찍는
대신 **최근 며칠을 되돌아보며 빈 날을 채우는** 방식이어야 한다(collector/backfill.py
의 ``recent`` 모드).
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

import requests

from collector import config, raw_store
from collector.ratelimit import RateLimiter

#: 응답 한 줄이 주는 필드. 실측 2026-09-19 기준 15개이며 전부 문자열로 온다.
FIELDS = ("basDt", "srtnCd", "isinCd", "itmsNm", "mrktCtg", "clpr", "vs", "fltRt",
          "mkp", "hipr", "lopr", "trqu", "trPrc", "lstgStCnt", "mrktTotAmt")


class PortalError(RuntimeError):
    """포털이 정상 응답을 주지 않았다."""


@dataclass
class DayResult:
    bas_dt: str
    status: str          # done | empty | error
    rows: int
    sha256: str = ""
    message: str = ""


# ==================================================
# 1. 파싱
# ==================================================
def _num(v, cast=int):
    """문자열 숫자를 파싱한다. 빈 값은 ``None``.

    ⚠️ 등락률이 ``"-.39"`` 처럼 **0 을 생략한 꼴**로 온다. ``float`` 는 이것을 받지만,
       직접 자릿수를 세는 파서를 짜면 여기서 깨진다. 그래서 그냥 float 에 맡긴다.
    """
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s == "":
        return None
    try:
        return int(float(s)) if cast is int else cast(s)
    except (TypeError, ValueError):
        return None


def is_halted(item: Dict) -> bool:
    """그날 **거래가 없었는가**.

    포털에서는 시가와 거래량이 함께 0 으로 온다 — 실측 2026-09-17 하루치에서
    ``mkp==0`` 147 종목, ``trqu==0`` 147 종목, 교집합 147 로 **완전히 일치**했다.
    둘 다 보는 것은 언젠가 한쪽만 0 으로 오는 날을 놓치지 않기 위해서다.

    ⚠️ 판별 규칙이 **출처마다 다르다.** KIS 는 시가 필드가 아니라 누적거래량
       ``acml_vol==0`` 으로 나타난다(#33). 출처를 섞어 쓸 때 한쪽 규칙을 다른 쪽에
       적용하면 정지일을 통째로 놓친다.
    """
    return _num(item.get("mkp")) in (0, None) and _num(item.get("trqu")) in (0, None)


def parse(body: bytes) -> Tuple[int, List[Dict]]:
    """응답 바이트에서 ``(totalCount, 행들)`` 을 꺼낸다.

    ⚠️ KIS 의 ``rt_cd`` 에 해당하는 ``resultCode`` 를 **먼저** 본다. 포털은 실패해도
       HTTP 200 으로 주는 경우가 있어, 상태 코드만 보면 조용히 틀린다.
    """
    doc = json.loads(body.decode("utf-8"))
    resp = doc.get("response") or {}
    header = resp.get("header") or {}
    code = str(header.get("resultCode", ""))
    if code != "00":
        raise PortalError(
            f"포털이 정상 응답이 아니다: resultCode={code!r} "
            f"resultMsg={header.get('resultMsg')!r}\n"
            "  할 일: 키가 만료됐거나 활용신청이 안 된 API 일 수 있다. 포털 마이페이지에서\n"
            "         해당 API 의 승인 상태와 키를 확인한다."
        )
    b = resp.get("body") or {}
    total = int(b.get("totalCount") or 0)
    if total == 0:
        return 0, []
    items = (b.get("items") or {}).get("item") or []
    if isinstance(items, dict):   # 1건일 때 dict 로 오는 API 가 있다
        items = [items]
    return total, items


# ==================================================
# 2. 하루치 받기
# ==================================================
def fetch_day(conn: sqlite3.Connection, limiter: RateLimiter, bas_dt: str, *,
              session: Optional[requests.Session] = None,
              keep_raw: bool = True) -> DayResult:
    """기준일 하루치 전종목을 받아 원문·정규화·상태를 **한 트랜잭션에** 쓴다.

    셋이 함께 커밋돼야 하는 이유: 원문만 남고 상태가 안 남으면 다음 실행이 같은 날을 또
    받아 유량을 두 배로 쓴다. 상태만 남고 원문이 안 남으면 재정규화가 불가능해진다.
    """
    limiter.check_budget()
    limiter.wait()
    sess = session or requests
    params = {
        "serviceKey": config.portal_key(),
        "numOfRows": config.PORTAL_ROWS,
        "pageNo": 1,
        "resultType": "json",
        "basDt": bas_dt,
    }
    try:
        r = sess.get(config.PORTAL_PRICE_URL, params=params, timeout=90)
    except requests.RequestException as exc:
        return DayResult(bas_dt, "error", 0, message=f"네트워크 실패: {exc}")

    limiter.observe(r.headers)
    body = r.content
    if r.status_code != 200:
        return DayResult(bas_dt, "error", 0, message=f"HTTP {r.status_code}")

    try:
        total, items = parse(body)
    except (PortalError, ValueError, UnicodeDecodeError) as exc:
        return DayResult(bas_dt, "error", 0, message=str(exc).split("\n")[0])

    # 조용히 자르지 않는다. numOfRows 를 넘으면 페이지가 더 있다는 뜻이고, 그것을 무시하면
    # 그날 종목 일부가 영영 빠진 채 "성공" 으로 기록된다.
    if total > config.PORTAL_ROWS:
        raise PortalError(
            f"하루치가 한 번에 안 들어온다: totalCount={total:,} > numOfRows="
            f"{config.PORTAL_ROWS:,} ({bas_dt})\n"
            "  할 일: config.PORTAL_ROWS 를 올리거나 pageNo 순회를 넣는다.\n"
            "         그 전까지 이 날은 **수집된 것으로 표시하지 않는다.**"
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        sha = ""
        if keep_raw:
            sha = raw_store.save(conn, "portal", f"price/{bas_dt}", body,
                                 http_status=r.status_code,
                                 note=f"rows={len(items)}")
        n = _upsert_rows(conn, items, sha) if items else 0
        status = "done" if n else "empty"
        _mark_day(conn, bas_dt, status, n, "")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return DayResult(bas_dt, status, n, sha)


def _upsert_rows(conn: sqlite3.Connection, items: List[Dict], sha: str) -> int:
    rows = []
    for it in items:
        rows.append((
            it.get("basDt", ""), it.get("srtnCd", ""), it.get("isinCd", ""),
            it.get("itmsNm", ""), it.get("mrktCtg", ""),
            _num(it.get("clpr")), _num(it.get("vs")), _num(it.get("fltRt"), float),
            _num(it.get("mkp")), _num(it.get("hipr")), _num(it.get("lopr")),
            _num(it.get("trqu")), _num(it.get("trPrc")),
            _num(it.get("lstgStCnt")), _num(it.get("mrktTotAmt")),
            1 if is_halted(it) else 0, sha,
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO price_daily "
        "(bas_dt,srtn_cd,isin_cd,itms_nm,mrkt_ctg,clpr,vs,flt_rt,mkp,hipr,lopr,"
        " trqu,tr_prc,lstg_st_cnt,mrkt_tot_amt,halted,raw_sha256) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def _mark_day(conn: sqlite3.Connection, bas_dt: str, status: str, rows: int,
              message: str) -> None:
    """상태를 남긴다. 시도 횟수는 **누적**한다 — empty 를 몇 번 겪었는지가
    휴장 확정의 근거이기 때문이다."""
    conn.execute(
        "INSERT INTO ingest_day (source,bas_dt,status,rows,attempts,updated_at,message) "
        "VALUES ('portal',?,?,?,1,?,?) "
        "ON CONFLICT(source,bas_dt) DO UPDATE SET "
        "  status=excluded.status, rows=excluded.rows, "
        "  attempts=ingest_day.attempts+1, updated_at=excluded.updated_at, "
        "  message=excluded.message",
        (bas_dt, status, rows, raw_store.now_kst_iso(), message))


def mark_error(conn: sqlite3.Connection, res: DayResult) -> None:
    """실패를 기록한다. 실패도 남겨야 다음에 **그 날만** 다시 받는다."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _mark_day(conn, res.bas_dt, "error", 0, res.message[:400])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ==================================================
# 3. 날짜 목록
# ==================================================
def weekdays(start: date, end: date) -> List[str]:
    """주말을 뺀 날짜 목록(YYYYMMDD, 과거→현재).

    공휴일은 **거르지 않는다.** 한국 증시 휴장일 달력을 따로 들고 있지 않고, 틀린 달력은
    있는 것보다 나쁘기 때문이다. 공휴일은 호출해 보면 0건으로 오고, 그것은 ``empty`` 로
    기록된 뒤 재시도 한도를 넘기면 ``holiday`` 로 확정된다.

    주말만 걸러도 호출의 약 28% 가 준다 — 이건 달력 없이 확실히 아는 사실이다.
    """
    out: List[str] = []
    cur = start
    one = _dt.timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y%m%d"))
        cur += one
    return out
