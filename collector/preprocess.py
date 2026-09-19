"""전처리 규격 v1 — 수정주가·거래정지·상장폐지.

이 파일이 답하는 질문은 하나다: **"과거 가격을 오늘 기준으로 어떻게 고쳐 읽을 것인가."**

왜 고쳐 읽어야 하나 (비전문가용 한 문단)
----------------------------------------
액면분할은 1주를 여러 주로 쪼개는 일이다. 카카오는 2021년 4월 1주를 5주로 쪼갰고, 주가는
558,000 원에서 112,000 원대로 **뚝 떨어진 것처럼** 보인다. 회사 가치는 그대로인데 숫자만
1/5 이 된 것이다. 이걸 그대로 두고 "전일 대비 수익률" 을 계산하면 그날 -80% 짜리 대폭락이
있었던 것으로 기록된다. 그런 가짜 폭락이 섞인 데이터로 전략을 검증하면, 검증 결과 자체가
거짓이 된다. 그래서 과거 가격에 **조정계수**를 곱해 이어 붙인다.

용어 두 층
----------
**액면분할(株式分割, stock split)**
  뜻 — 1주를 n주로 쪼개는 것. 주식 수가 n배가 되고 주당 가격은 1/n 이 된다.
  이 문서에서 — 가격이 끊기는 날의 대표 사례. 조정이 필요한 이유 그 자체.
  헷갈리는 점 — "가격이 싸졌다" 가 아니다. 피자를 8조각에서 16조각으로 자른 것이지
  피자가 커지거나 작아진 게 아니다.

**조정계수(adjustment factor)**
  뜻 — 과거 가격에 곱해서 오늘 기준으로 맞추는 배수.
  이 문서에서 — ``기준가 ÷ 직전 종가``. 카카오는 112,000 ÷ 558,000 = 0.2007.
  헷갈리는 점 — 1/5 = 0.2 와 **정확히 같지 않다.** 아래를 본다.

**기준가(基準價, base price)**
  뜻 — 분할·병합 뒤 첫 거래일에 거래소가 정해 주는 출발 가격.
  이 문서에서 — ``종가 − 전일대비``로 역산해 얻는다.
  헷갈리는 점 — 단순히 ``직전 종가 ÷ 분할비율`` 이 **아니다.** 카카오는
  558,000 ÷ 5 = 111,600 이 아니라 **112,000** 이었다. 호가 단위에 맞춰 조정되기
  때문이다. 그래서 **직접 계산하지 않고 데이터가 말해 주는 값을 쓴다.**

왜 등락률(fltRt)이 아니라 전일대비(vs)인가  ★ v1 에서 바뀐 부분
---------------------------------------------------------------
S26 까지의 규격은 ``fltRt``(등락률 %) 를 누적하는 것이었다. 실측해 보니 그쪽이 덜
정확하다. ``fltRt`` 는 **소수 둘째 자리까지만** 온다 — 삼성전자 2026-09-17 은 실제
−0.3945% 인데 ``"-.39"`` 로 잘려 온다. 반면 ``vs`` 는 정수 원 단위라 자르는 일이 없다.

실측 (카카오 035720, 2021-04-08 ~ 04-22)::

    기준일        vs 로 역산한 전일가   fltRt 로 역산한 전일가   실제 직전 종가
    20210409          548,000.0             548,025.9            548,000  ← vs 만 정확
    20210415          112,000.0             111,999.3            558,000  ← 분할일
    20210421          119,500.0             119,505.8            119,500  ← vs 만 정확

열 곳 중 ``vs`` 는 열 곳 모두 소수점 이하 0 으로 정확히 맞았고, ``fltRt`` 는 매번 몇십 원씩
어긋났다. 하루 오차가 0.005% 라도 1,648 일을 곱해 나가면 누적된다.

그리고 분할일 행을 보면 ``vs`` 가 **이미 조정된 기준가를 기준으로** 계산돼 있다
(120,500 − 8,500 = 112,000). 즉 조정계수를 우리가 추정할 필요가 없다 — **데이터 안에
들어 있다.**

한계 — 무엇이 아직 확인되지 않았나
----------------------------------
🟢 액면분할 1건(카카오 1:5)에서 완전 일치.
🟡 **유상증자 권리락·주식병합은 아직 확인하지 못했다.** 같은 방식이 통할 것으로 보지만
   근거가 없다. 이 규격을 뒤집을 조건: 권리락일의 ``clpr − vs`` 가 직전 종가와도 다르고
   거래소 기준가와도 다르면, ``vs`` 기반 조정은 배당락·권리락을 못 잡는 것이다.
🔴 **배당은 이 조정에 들어 있지 않다.** 여기서 만드는 것은 가격수익(PR) 계열이지
   총수익(TR) 이 아니다. 배당까지 반영하려면 별도 배당 자료가 필요하다(#33 열린 질문).
"""

from __future__ import annotations

import sqlite3
from typing import Dict, Iterable, List, Optional, Tuple

from collector import db

#: 이보다 더 어긋나면 "가격이 끊긴 날" 로 본다.
#:
#: ``vs`` 가 정수라 정상일의 계수는 **정확히 1.0** 이 된다. 그래서 임계값은 거의 0 이어도
#: 되지만, 부동소수 나눗셈의 마지막 자리를 감안해 아주 작은 여유만 둔다.
EVENT_EPS = 1e-9

#: 계수가 이보다 크게 어긋나면 이벤트가 아니라 **데이터 이상**을 의심한다.
#: 1:10 분할이면 0.1, 10:1 병합이면 10 이다. 그 바깥은 사람이 봐야 한다.
SANITY_LO, SANITY_HI = 0.005, 200.0

#: 가격 계수와 상장주식수 비율이 이보다 어긋나면 **사람이 확인해야 한다.**
#:
#: 왜 이런 검사를 두나 — 조정계수를 확인해 줄 **두 번째 독립 신호**가 데이터 안에 있다.
#: 액면분할이면 주식 수가 늘어난 배수만큼 가격이 내려가므로, 가격 계수 f 와 주식수 비율
#: q 를 곱하면 1 이 되어야 한다.
#:
#: 실측 2026-09-19 — 이 검사가 실제로 두 건을 골라냈다::
#:
#:     카카오   20210415  f=0.200717  q=5.00   f·q=1.004   OK
#:     세기상사 20210421  f=0.100000  q=10.00  f·q=1.000   OK
#:     한국석유 20210415  f=0.051779  q=10.00  f·q=0.518   ← 어긋남
#:     미트박스 20260916  f=0.333534  q=1.00   f·q=0.334   ← 어긋남
#:
#: 두 건의 성격이 다르다는 점이 중요하다.
#:   · 한국석유는 281,000 원이 1/10 인 28,100 이 아니라 **14,550** 으로 다시 시작했다.
#:     분할만으로 설명되지 않는다 — 다른 사건이 겹쳤을 수 있다. **자동으로 판단하지 않는다.**
#:   · 미트박스는 가격만 1/3 이 되고 상장주식수는 그대로였다. 포털의 ``lstgStCnt`` 가
#:     **늦게 갱신된** 것으로 보인다. 즉 주식수 쪽이 틀렸고 가격 계수가 맞다.
#:
#: 그래서 이 검사는 **둘 중 어느 쪽이 맞다고 정하지 않는다.** 표시만 하고 사람에게 넘긴다.
LSTG_CROSS_TOL = 0.05

#: 주식수 비율이 이 범위 안이면 "주식수는 그대로" 로 본다 — 권리락 판정에 쓴다.
LSTG_SAME_TOL = 0.01

#: 이벤트 분류. 자동으로 고칠 수 있는 것과 사람이 봐야 하는 것을 가른다.
#:
#: ``split``    가격 계수와 주식수 비율이 서로 맞는다 (f·q ≈ 1). 액면분할·병합.
#:              두 신호가 독립적으로 같은 말을 하므로 **자동 조정을 믿어도 된다.**
#: ``rights``   주식수는 그대로인데 가격만 끊겼다 (q ≈ 1, f ≠ 1). 권리락이다.
#:              무상·유상증자는 신주가 **나중에** 상장되므로 권리락일에는 주식수가
#:              아직 안 늘어난다. 실측 2026-09-19::
#:
#:                  씨젠     20210423  195,000 → 기준가 98,000  f=0.50256  주식수 불변
#:                  대한제당 20210429    7,490 → 기준가  3,745  f=0.50000  주식수 불변
#:
#:              **이것이 S26 의 열린 질문 "권리락에서도 vs 가 통하는가" 에 대한 답이다.**
#:              통한다. 다만 주식수로 교차검증할 수는 없다.
#: ``review``   두 신호가 **모두** 어긋난다. 자동으로 판단하지 않는다. 실측 사례::
#:
#:                  한국석유 20210415  281,000 → 14,550  f=0.0518  주식수 ×10  (1/10 이면 28,100)
#:                  아이엠텍 20210408      214 →  5,350  f=25.0    주식수 ÷9.01
#:
#:              아이엠텍은 사각지대를 그대로 보여 준다 — 그 구간이 **거래정지라 vs 가
#:              계속 0** 이었다. vs 가 0 이면 "가격이 이어진다" 와 "조정 정보가 없다" 가
#:              구별되지 않는다. 정지 해제일에 가격이 달라져 있으면, 그것이 조정 때문인지
#:              실제 등락인지 이 데이터만으로는 알 수 없다.
EV_SPLIT, EV_RIGHTS, EV_REVIEW = "split", "rights", "review"


def classify(f: float, q):
    """이벤트 한 건을 분류한다. ``(종류, 교차값)``.

    ``q`` 가 ``None`` 인 것(주식수 정보 없음)은 통과가 아니라 **확인 필요**다 —
    검사를 못 한 것과 검사를 통과한 것은 다르다.
    """
    if q is None:
        return EV_REVIEW, None
    cross = f * q
    if abs(cross - 1.0) <= LSTG_CROSS_TOL:
        return EV_SPLIT, cross
    if abs(q - 1.0) <= LSTG_SAME_TOL:
        return EV_RIGHTS, cross
    return EV_REVIEW, cross


def _series(conn: sqlite3.Connection, code: str) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT bas_dt, clpr, vs, mkp, hipr, lopr, halted, itms_nm, lstg_st_cnt "
        "FROM price_daily WHERE srtn_cd=? ORDER BY bas_dt", (code,)))


def factors(rows: List[sqlite3.Row]) -> List[Tuple[str, float, Optional[Dict]]]:
    """날짜별 ``(기준일, 그날의 조정계수, 이벤트 정보)``.

    계수는 **그날 가격이 직전과 이어지는가**를 나타낸다.

        계수 = (종가 − 전일대비) ÷ 직전 거래일 종가

    이어지면 1.0, 분할이면 1 보다 작고, 병합이면 1 보다 크다.

    첫 날은 비교 대상이 없으므로 1.0 으로 둔다. 상장 첫날의 ``vs`` 는 공모가 대비인
    경우가 있는데, 그걸 조정으로 읽으면 상장일에 가짜 이벤트가 생긴다.
    """
    out: List[Tuple[str, float, Optional[Dict]]] = []
    prev_clpr: Optional[int] = None
    prev_lstg: Optional[int] = None
    for r in rows:
        clpr, vs = r["clpr"], r["vs"]
        f, ev = 1.0, None
        if prev_clpr and clpr is not None and vs is not None and prev_clpr > 0:
            base = clpr - vs
            if base > 0:
                f = base / prev_clpr
                if abs(f - 1.0) > EVENT_EPS:
                    # 두 번째 독립 신호: 주식수가 q 배로 늘면 가격은 1/q 이어야 한다.
                    q = (r["lstg_st_cnt"] / prev_lstg) if (prev_lstg and r["lstg_st_cnt"]) else None
                    kind, cross = classify(f, q)
                    ev = {
                        "bas_dt": r["bas_dt"], "itms_nm": r["itms_nm"],
                        "prev_clpr": prev_clpr, "base_price": base, "factor": f,
                        "lstg_before": prev_lstg, "lstg_after": r["lstg_st_cnt"],
                        "lstg_cross": cross, "kind": kind,
                        "needs_review": 1 if kind == EV_REVIEW else 0,
                    }
        out.append((r["bas_dt"], f, ev))
        if clpr:
            prev_clpr = clpr
        if r["lstg_st_cnt"]:
            prev_lstg = r["lstg_st_cnt"]
    return out


def cumulative(day_factors: List[Tuple[str, float, Optional[Dict]]]) -> Dict[str, float]:
    """각 날에 곱할 **누적** 계수. 가장 최근 날이 1.0 이다.

    뒤에서 앞으로 훑는 이유: 오늘 가격을 건드리지 않고 과거만 고치기 위해서다. 앞에서
    뒤로 가면 오늘 가격이 바뀌어, 어제 본 화면과 오늘 본 화면의 현재가가 달라진다.
    """
    cum: Dict[str, float] = {}
    c = 1.0
    for d, f, _ in reversed(day_factors):
        cum[d] = c
        c *= f       # 그날의 끊김은 '그 이전' 전부에 적용된다
    return cum


def rebuild(conn: sqlite3.Connection, codes: Optional[Iterable[str]] = None,
            *, verbose: bool = False) -> Dict[str, int]:
    """수정주가와 조정 이벤트를 다시 만든다. **몇 번을 돌려도 같은 결과**가 된다."""
    if codes is None:
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT srtn_cd FROM price_daily ORDER BY srtn_cd")]
    codes = list(codes)

    tally = {"codes": 0, "rows": 0, "events": 0, "suspect": 0,
             EV_SPLIT: 0, EV_RIGHTS: 0, EV_REVIEW: 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for code in codes:
            rows = _series(conn, code)
            if not rows:
                continue
            ff = factors(rows)
            cum = cumulative(ff)

            adj, evs = [], []
            for r in rows:
                c = cum[r["bas_dt"]]
                adj.append((
                    r["bas_dt"], code,
                    r["clpr"] * c if r["clpr"] is not None else None,
                    r["mkp"] * c if r["mkp"] else None,
                    r["hipr"] * c if r["hipr"] else None,
                    r["lopr"] * c if r["lopr"] else None,
                    c,
                ))
            for _, _, ev in ff:
                if ev is None:
                    continue
                if not (SANITY_LO <= ev["factor"] <= SANITY_HI):
                    tally["suspect"] += 1
                    if verbose:
                        print(f"  ⚠️ 계수가 상식 밖이다 — {code} {ev['bas_dt']} "
                              f"factor={ev['factor']:.6g} (사람이 확인해야 한다)")
                if ev["needs_review"]:
                    if verbose:
                        c = ev["lstg_cross"]
                        print(f"  ⚠️ 확인 필요 — {code} {ev['itms_nm']} {ev['bas_dt']} "
                              f"계수 {ev['factor']:.6f} · 주식수비 교차 "
                              + (f"{c:.3f}" if c is not None else "(주식수 정보 없음)"))
                tally[ev["kind"]] = tally.get(ev["kind"], 0) + 1
                evs.append((ev["bas_dt"], code, ev["itms_nm"], ev["prev_clpr"],
                            ev["base_price"], ev["factor"],
                            ev["lstg_before"], ev["lstg_after"],
                            ev["lstg_cross"], ev["needs_review"], ev["kind"]))

            conn.execute("DELETE FROM price_adjusted WHERE srtn_cd=?", (code,))
            conn.executemany(
                "INSERT INTO price_adjusted "
                "(bas_dt,srtn_cd,adj_clpr,adj_mkp,adj_hipr,adj_lopr,cum_factor) "
                "VALUES (?,?,?,?,?,?,?)", adj)
            conn.execute("DELETE FROM corporate_action WHERE srtn_cd=?", (code,))
            if evs:
                conn.executemany(
                    "INSERT INTO corporate_action "
                    "(bas_dt,srtn_cd,itms_nm,prev_clpr,base_price,factor,lstg_before,lstg_after,"
                    " lstg_cross,needs_review,kind) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", evs)
            tally["codes"] += 1
            tally["rows"] += len(adj)
            tally["events"] += len(evs)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return tally


# ==================================================
# 상장폐지 — 지우지 않는다
# ==================================================
def delisted(conn: sqlite3.Connection, *, as_of: Optional[str] = None) -> List[sqlite3.Row]:
    """최근 기준일에 없는 종목 = 그 사이 사라진 종목.

    **지우지 않는 것이 요점이다.** 사라진 종목을 빼고 과거를 보면 "끝까지 살아남은
    종목들" 만 남아 수익률이 실제보다 좋아 보인다. 이것을 생존 편향(survivorship bias)
    이라고 하고, 전략 검증을 통째로 무의미하게 만드는 가장 흔한 원인이다.

    그래서 수집기는 사라진 종목의 과거 행을 **그대로 둔다.** 이 함수는 그 목록을
    보여 줄 뿐 아무것도 지우지 않는다.

    ⚠️ 여기서 나오는 목록은 "상장폐지" 와 정확히 같지 않다. 이전·합병·코스닥 이전상장도
       섞인다. 사유를 알려면 별도 자료(KIND)가 필요하다 — #33 의 열린 질문이다.
    """
    last = as_of or conn.execute("SELECT MAX(bas_dt) FROM price_daily").fetchone()[0]
    if not last:
        return []
    return list(conn.execute(
        "SELECT srtn_cd, itms_nm, mrkt_ctg, MAX(bas_dt) AS last_seen, COUNT(*) AS days "
        "FROM price_daily GROUP BY srtn_cd HAVING last_seen < ? "
        "ORDER BY last_seen DESC", (last,)))


def main() -> int:
    conn = db.connect()
    print("수정주가 다시 계산 중 …")
    t = rebuild(conn, verbose=True)
    print(f"  종목 {t['codes']:,} · 행 {t['rows']:,} · 조정 이벤트 {t['events']:,}")
    print(f"    분할·병합(두 신호 일치) {t[EV_SPLIT]:,} · "
          f"권리락(주식수 불변) {t[EV_RIGHTS]:,} · "
          f"⚠️ 확인 필요 {t[EV_REVIEW]:,}"
          + (f" · ⚠️ 상식 밖 {t['suspect']:,}" if t["suspect"] else ""))
    d = delisted(conn)
    print(f"  최근 기준일에 없는 종목 {len(d):,} (지우지 않는다 — 생존 편향)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
