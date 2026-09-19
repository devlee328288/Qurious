"""배당 수집 — 달 단위로 훑고, 중단을 전제로 만든다.

무엇을 하나
-----------
DART 「현금ㆍ현물배당결정」 공시를 배당철만 훑어 ``dividend`` 표를 채운다. 규칙과 근거는
``collector/sources/dart.py`` 머리말에 있다. 이 파일은 **순서와 재개**를 맡는다.

네 가지 모드
------------
``corp-code``  종목코드↔기업고유번호 매핑을 만든다. 호출 1회. 처음에 한 번.
``scan``       배당철 달을 훑는다. 이미 훑은 달은 건너뛴다.
``reparse``    **네트워크를 한 번도 안 타고** 보존 원문으로 다시 파싱한다.
``status``     지금까지 무엇이 쌓였는지.

``reparse`` 가 있는 이유가 이 설계의 요점이다. 파싱 규칙은 틀리고, 고치면 전부 다시
읽어야 한다. 원문을 남겨 두었으므로(``raw_response``) 그때 하루 한도를 쓰지 않는다.
실제로 이 수집기를 만들면서 파서를 두 번 고쳤다.

재개는 어디에 기대나
--------------------
두 층이다.

1. **달** — ``ingest_day`` 의 ``source='dart_dividend'``. 끝까지 훑은 달만 ``done``.
   중간에 멈춘 달은 안 남기므로 다음 실행이 그 달을 다시 훑는다(목록 호출만 손해).
2. **공시 한 건** — ``raw_response`` 에 원문이 있으면 **네트워크를 안 탄다.** 그래서
   1번이 다시 훑어도 본문은 다시 받지 않는다. 손해가 목록 호출로만 한정된다.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

from collector import config, db, raw_store
from collector.sources import dart
from collector.ratelimit import RateLimiter

#: 훑을 연도 범위 기본값. 시세 자료가 2020-01-02 부터라 그 앞은 배당락일을 못 구한다.
#: 다만 2019 년 12월 결산배당의 공시는 **2020년 2~3월에 나오므로** 시작 연도는 2020 이다.
DEFAULT_FROM_YEAR = 2020


def _limiter() -> RateLimiter:
    return RateLimiter(config.DART_SLEEP, reserve=config.DART_RESERVE, name="DART")


# ==================================================
# 1. 한 달 처리
# ==================================================
def process_month(conn: sqlite3.Connection, limiter: RateLimiter, ym: str, *,
                  session: Optional[requests.Session] = None,
                  code_by_corp: Optional[Dict[str, str]] = None,
                  cal: Optional[List[str]] = None,
                  max_calls: Optional[int] = None,
                  quiet: bool = False) -> Dict[str, int]:
    """한 달: 목록 훑기 → 본문 받기 → 파싱 → 저장 → 상태 남기기."""
    code_by_corp = code_by_corp if code_by_corp is not None else _code_index()
    cal = cal if cal is not None else dart.trading_days(conn)

    scan = dart.scan_month(conn, limiter, ym, session=session, max_calls=max_calls)
    tally = {"list_calls": scan.list_calls, "reports": scan.total_reports,
             "candidates": len(scan.rows), "excluded": len(scan.excluded),
             "no_code": 0, "no_record_dt": 0, "subsidiary": 0, "no_file": 0,
             "review": 0, "saved": 0}

    items: List[dart.Dividend] = []
    for row in scan.rows:
        corp = row.get("corp_code") or ""
        code = code_by_corp.get(corp, "")
        if not code:
            # 상장사가 아니거나 매핑 파일이 만들어진 뒤 상장한 종목이다. 시세 표에 없으므로
            # 배당을 붙일 곳이 없다 — 세어서 보고만 한다.
            tally["no_code"] += 1
            continue
        if max_calls is not None and limiter.calls >= max_calls:
            raise dart.DartQuotaExceeded(
                f"이번 실행의 호출 상한({max_calls:,}회)에 닿았다 — {ym} 본문 수집 중 멈췄다.\n"
                "  할 일: 다시 돌리면 이 달을 다시 훑되, 이미 받은 본문은 보존본을 쓰므로\n"
                "         목록 호출만 다시 든다."
            )
        try:
            xml = dart.fetch_document(conn, limiter, row["rcept_no"], session=session)
        except dart.DartNoDocument:
            # 우리가 고칠 수 없는 결손이다. 한 건 때문에 전체를 멈추지 않고 세어 둔다.
            tally["no_file"] += 1
            continue
        d = dart.parse_document(xml)
        d.srtn_cd = code
        d.corp_code = corp
        d.itms_nm = row.get("corp_name") or ""
        d.rcept_no = row["rcept_no"]
        d.report_nm = row.get("report_nm") or ""
        kept = raw_store.load(conn, "dart", f"dividend/{d.rcept_no}")
        d.raw_sha256 = kept["sha256"] if kept else ""

        if "자회사" in d.note:
            tally["subsidiary"] += 1
            continue
        if not d.record_dt:
            # 기본키가 (종목, 기준일)이라 기준일 없이는 담을 수 없다. 담을 수 없는 것을
            # 빈 키로 억지로 넣으면 서로를 덮어쓴다 — 세어서 보고만 한다.
            tally["no_record_dt"] += 1
            continue
        ex = dart.ex_dividend_date(cal, d.record_dt)
        d.ex_div_dt = ex or ""
        if not ex:
            d.needs_review = 1
            d.note = (d.note + " / " if d.note else "") + \
                     f"배당락일 미정 — 기준일 {d.record_dt} 이 거래일 달력 밖이다"
        if d.needs_review:
            tally["review"] += 1
        items.append(d)

    conn.execute("BEGIN IMMEDIATE")
    try:
        tally["saved"] = dart.upsert(conn, items)
        dart.mark_month(
            conn, ym, "done", tally["saved"],
            f"수시공시 {scan.total_reports:,}건 중 대상 {len(scan.rows)}건 · "
            f"저장 {tally['saved']}건 · 검토 {tally['review']}건 · "
            f"제목제외 {tally['excluded']}건 · 자회사 {tally['subsidiary']}건 · "
            f"코드없음 {tally['no_code']}건 · 기준일없음 {tally['no_record_dt']}건 · "
            f"본문없음 {tally['no_file']}건")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    if not quiet:
        print(f"  {ym}  수시공시 {scan.total_reports:>6,}건 · 목록 {scan.list_calls:>3}쪽 → "
              f"대상 {len(scan.rows):>3}건 → 저장 {tally['saved']:>3}건"
              + (f" · 검토 {tally['review']}" if tally["review"] else "")
              + (f" · 자회사 {tally['subsidiary']}" if tally["subsidiary"] else "")
              + (f" · 코드없음 {tally['no_code']}" if tally["no_code"] else "")
              + (f" · 기준일없음 {tally['no_record_dt']}" if tally["no_record_dt"] else "")
              + (f" · 본문없음 {tally['no_file']}" if tally["no_file"] else ""))
    return tally


def _code_index() -> Dict[str, str]:
    """기업고유번호 → 종목코드. 공시는 corp_code 로 오고 시세 표는 종목코드다."""
    return {v[0]: k for k, v in dart.corp_code_map().items()}


# ==================================================
# 2. 러너
# ==================================================
def run_scan(*, from_year: int = DEFAULT_FROM_YEAR, to_year: Optional[int] = None,
             months: Optional[Tuple[int, ...]] = None, limit: Optional[int] = None,
             max_calls: Optional[int] = None, rescan: bool = False,
             quiet: bool = False) -> Dict[str, int]:
    """배당철을 훑는다. 한도에 닿으면 **깔끔히 멈추고** 지금까지를 보고한다."""
    to_year = to_year or datetime.now().year
    if months:
        wanted = [f"{y}{m:02d}" for y in range(from_year, to_year + 1) for m in months]
    else:
        wanted = dart.dividend_months(from_year, to_year)
    # 아직 오지 않은 달은 훑지 않는다
    now_ym = datetime.now().strftime("%Y%m")
    wanted = [ym for ym in wanted if ym <= now_ym]

    conn = db.connect()
    limiter = _limiter()
    session = requests.Session()
    states = dart.done_months(conn)
    todo = wanted if rescan else [ym for ym in wanted
                                  if (states.get(ym) or {"status": ""})["status"] != "done"]
    if limit:
        todo = todo[:limit]

    total = {"months": 0, "saved": 0, "review": 0, "candidates": 0, "excluded": 0,
             "subsidiary": 0, "no_code": 0, "no_record_dt": 0, "no_file": 0}
    if not todo:
        print(f"훑을 달이 없다 — 대상 {len(wanted)}달이 모두 처리돼 있다.")
        conn.close()
        return total

    done_n = sum(1 for ym in wanted
                 if (states.get(ym) or {"status": ""})["status"] == "done")
    print(f"대상 {len(todo)}달 (배당철 {len(wanted)}달 중 {done_n}달은 이미 처리됨"
          + (f" · --limit 로 {len(wanted) - done_n - len(todo)}달은 이번에 제외" if limit else "")
          + ")")
    code_by_corp = _code_index()
    cal = dart.trading_days(conn)
    stopped = ""
    for ym in todo:
        try:
            t = process_month(conn, limiter, ym, session=session,
                              code_by_corp=code_by_corp, cal=cal,
                              max_calls=max_calls, quiet=quiet)
        except dart.DartQuotaExceeded as exc:
            stopped = str(exc)
            break
        total["months"] += 1
        for k in ("saved", "review", "candidates", "excluded",
                  "subsidiary", "no_code", "no_record_dt", "no_file"):
            total[k] += t[k]

    conn.close()
    print(f"\n훑은 달 {total['months']}달 · 저장 {total['saved']:,}건 "
          f"(검토필요 {total['review']:,}건) · 제목제외 {total['excluded']:,}건 · "
          f"자회사 {total['subsidiary']:,}건 · 코드없음 {total['no_code']:,}건 · "
          f"기준일없음 {total['no_record_dt']:,}건 · 본문없음 {total['no_file']:,}건")
    print(limiter.report())
    if stopped:
        print(f"\n중단됨:\n{stopped}")
    return total


def run_reparse(*, quiet: bool = False) -> Dict[str, int]:
    """보존 원문으로 다시 파싱한다. **네트워크 0회.**

    종목코드·제목은 ``dividend`` 표에 이미 있는 값을 쓴다 — 목록을 다시 받지 않기
    위해서다. 그래서 이 모드는 **한 번이라도 scan 을 돈 뒤에만** 뜻이 있다.
    """
    conn = db.connect()
    cal = dart.trading_days(conn)
    rows = conn.execute(
        "SELECT srtn_cd, rcept_no, corp_code, itms_nm, report_nm FROM dividend "
        "WHERE rcept_no <> '' ORDER BY rcept_no").fetchall()
    tally = {"read": 0, "missing_raw": 0, "saved": 0, "review": 0, "dropped": 0}
    items: List[dart.Dividend] = []
    for r in rows:
        kept = raw_store.load(conn, "dart", f"dividend/{r['rcept_no']}")
        if not kept:
            tally["missing_raw"] += 1
            continue
        tally["read"] += 1
        d = dart.parse_document(dart._unzip(kept["body"], r["rcept_no"]))
        d.srtn_cd, d.rcept_no = r["srtn_cd"], r["rcept_no"]
        d.corp_code, d.itms_nm = r["corp_code"], r["itms_nm"]
        d.report_nm, d.raw_sha256 = r["report_nm"], kept["sha256"]
        if not d.record_dt:
            tally["dropped"] += 1
            continue
        d.ex_div_dt = dart.ex_dividend_date(cal, d.record_dt) or ""
        if d.needs_review:
            tally["review"] += 1
        items.append(d)

    conn.execute("BEGIN IMMEDIATE")
    try:
        tally["saved"] = dart.upsert(conn, items)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.close()
    print(f"보존 원문 {tally['read']:,}건 재파싱 → 저장 {tally['saved']:,}건 "
          f"(검토필요 {tally['review']:,}건 · 기준일없음 {tally['dropped']:,}건 · "
          f"원문없음 {tally['missing_raw']:,}건)")
    if tally["dropped"]:
        print("  ⚠️ 기준일을 못 읽은 건은 표에 담기지 않는다 — 이전 값이 남아 있을 수 있다.")
    return tally


# ==================================================
# 3. 현황
# ==================================================
def print_status() -> None:
    conn = db.connect()
    rows = conn.execute(
        "SELECT status, COUNT(*) n, SUM(rows) r FROM ingest_day "
        "WHERE source='dart_dividend' GROUP BY status").fetchall()
    print("― 배당 수집 현황 ―")
    if not rows:
        print("  아직 아무것도 훑지 않았다. `python -m collector.dividend scan` 으로 시작한다.")
    for r in rows:
        print(f"  {r['status']:<8} {r['n']:>4}달  {(r['r'] or 0):>8,}건")

    s = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT srtn_cd) c, MIN(record_dt) a, MAX(record_dt) b, "
        "SUM(needs_review) rev, SUM(ex_div_dt='') noex FROM dividend").fetchone()
    if s and s["n"]:
        print(f"\n― dividend ―\n  {s['n']:,}건 · 종목 {s['c']:,}개 · "
              f"기준일 {s['a']} ~ {s['b']}")
        print(f"  검토필요 {s['rev'] or 0:,}건 · 배당락일 미정 {s['noex'] or 0:,}건")
        kinds = conn.execute(
            "SELECT div_kind, COUNT(*) n FROM dividend GROUP BY div_kind ORDER BY n DESC").fetchall()
        print("  배당구분: " + " · ".join(f"{k['div_kind'] or '(빈칸)'} {k['n']:,}" for k in kinds))
        yr = conn.execute(
            "SELECT SUBSTR(record_dt,1,4) y, COUNT(*) n, COUNT(DISTINCT srtn_cd) c "
            "FROM dividend GROUP BY y ORDER BY y").fetchall()
        print("  연도별: " + " · ".join(f"{r['y']} {r['n']:,}건/{r['c']:,}종목" for r in yr))
    conn.close()


# ==================================================
# 4. CLI
# ==================================================
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m collector.dividend",
        description="DART 배당 공시 수집 (중단·재개 가능)")
    p.add_argument("mode", choices=["corp-code", "scan", "reparse", "status"],
                   help="corp-code=종목코드 매핑 만들기(1회) / scan=배당철 훑기 / "
                        "reparse=보존 원문 재파싱(네트워크 0회) / status=현황")
    p.add_argument("--from-year", type=int, default=DEFAULT_FROM_YEAR)
    p.add_argument("--to-year", type=int)
    p.add_argument("--months", help="훑을 달을 직접 지정 (예: 2,3). 기본은 배당철 전부")
    p.add_argument("--limit", type=int, help="이번 실행에서 훑을 최대 달 수 — 시험 삼아 돌릴 때")
    p.add_argument("--max-calls", type=int,
                   default=config.DART_DAILY_LIMIT - config.DART_RESERVE,
                   help="이번 실행의 호출 상한. DART 는 남은 유량을 알려 주지 않아 직접 센다")
    p.add_argument("--rescan", action="store_true",
                   help="이미 훑은 달도 다시 훑는다 (본문은 보존본을 써서 목록만 다시 든다)")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)

    if a.mode == "corp-code":
        limiter = _limiter()
        m = dart.build_corp_code_map(limiter)
        print(f"종목코드 매핑 {len(m):,}건 → {config.DART_CORP_CODE_PATH}")
        print(limiter.report())
        return 0
    if a.mode == "status":
        print_status()
        return 0
    if a.mode == "reparse":
        run_reparse(quiet=a.quiet)
        return 0

    months = tuple(int(x) for x in a.months.split(",")) if a.months else None
    run_scan(from_year=a.from_year, to_year=a.to_year, months=months,
             limit=a.limit, max_calls=a.max_calls, rescan=a.rescan, quiet=a.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
