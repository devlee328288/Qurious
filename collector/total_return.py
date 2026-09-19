"""총수익(TR) 계열 — 가격에 배당을 더해 "실제로 번 돈"을 만든다.

이 파일이 답하는 질문은 하나다: **"배당까지 세면 얼마나 벌었나."**

왜 필요한가 (비전문가용 한 문단)
--------------------------------
주가 차트는 배당을 보여 주지 않는다. 삼성전자를 2020년에 사서 2026년까지 들고 있었다면
주가가 오른 만큼만 번 것이 아니라, 그 사이 스물몇 번 받은 배당금도 벌었다. 그런데
``price_adjusted``(수정주가)는 분할·권리락만 고칠 뿐 배당은 손대지 않는다 — 손댈 수가
없다. **거래소가 현금배당으로는 기준가를 조정하지 않기 때문이다.** 실측하면 이렇다::

    삼성전자 20241227 (배당락일, 주당 363원)
      전일 종가 53,600 · 당일 종가 53,700 · 전일대비 vs = +100
      → 기준가 = 53,700 − 100 = 53,600 = 전일 종가 그대로

``vs`` 가 배당을 전혀 모르므로, ``vs`` 로 만든 수정주가도 배당을 모른다. 그래서 배당은
**가격에 섞는 것이 아니라 수익률에 더해야** 하고, 그 일을 이 파일이 한다.

용어 두 층
----------
**PR (Price Return, 가격수익)**
  뜻 — 가격 변동만으로 계산한 수익. 우리가 이미 가진 것(``price_adjusted``).
  이 문서에서 — ``pr_index`` 칸. 비교 대상으로 나란히 둔다.
  헷갈리는 점 — "수정주가" 는 PR 이다. 분할을 고쳤다고 총수익이 되는 게 아니다.

**TR (Total Return, 총수익)**
  뜻 — 가격 변동 + 배당을 함께 센 수익.
  이 문서에서 — ``tr_index`` 칸. 배당락일에 배당금으로 **그 종목을 더 샀다고 가정**한다.
  헷갈리는 점 — 배당금을 그냥 현금으로 쌓아 두는 것이 아니다. 재투자가 표준이다
  (S&P·MSCI·KRX TR 지수 전부 이 가정을 쓴다). 재투자 가정이 없으면 "배당을 받아서
  뭘 했는지" 가 사람마다 달라져 비교가 불가능해진다.

**배당락(配當落, ex-dividend)**
  뜻 — 이 날부터 사면 이번 배당을 못 받는다는 경계일.
  이 문서에서 — ``dividend.ex_div_dt``. 이 날 계수를 곱한다.
  헷갈리는 점 — 배당락일에 **주가가 배당금만큼 떨어지는 것이 이론**이지만, 한국 시장의
  실측 기울기는 0.703 이었다(#33 검증 ③ · 8,058건). 이론값 1.0 도, 세후 기대치 0.846 도
  아니다. **그래서 TR 은 실제 주가 하락폭이 아니라 공시된 배당금으로 계산한다** —
  하락폭으로 계산하면 시장의 변덕이 섞여 들어온다.

**재투자 가정(reinvestment assumption)**
  뜻 — 받은 배당금으로 같은 종목을 그날 종가에 다시 산다는 가정.
  이 문서에서 — ``div_factor = 1 + DPS ÷ 그날 종가``. 주식 수가 그 배수만큼 늘어난다.
  헷갈리는 점 — 실제로는 배당금이 배당락일이 아니라 **두세 달 뒤**에 들어온다(결산배당은
  주주총회 뒤). 그 시차를 무시하는 것이 표준이고, 이 계열도 무시한다. **한계 절 참고.**

계산 규칙 — 왜 이 식인가
------------------------
표준 TR 의 정의는 이렇다::

    그날 수익률 = (당일 종가 + 그날 받은 배당금) ÷ 전일 종가 − 1

이 식을 둘로 쪼개면 우리가 이미 가진 것과 맞물린다::

    (P_d + D_d) ÷ P_{d-1}  =  (P_d ÷ P_{d-1})  ×  (1 + D_d ÷ P_d)
                                └─ 가격 수익 ─┘    └── 배당 계수 ──┘

왼쪽 괄호는 ``price_adjusted`` 가 이미 분할·권리락을 고쳐 둔 값의 비율이고, 오른쪽
괄호는 이 파일이 새로 만드는 값이다. **둘을 곱하기만 하면 된다.**

★ 이 쪼개기에는 공짜 이득이 하나 더 있다. ``D_d ÷ P_d`` 는 **같은 날의 두 값**을 나눈
것이라 분할 조정과 무관하다. 1:5 분할이 있었다면 배당금도 주가도 똑같이 1/5 이 되므로
비율은 그대로다. 그래서 **배당금을 분할에 맞춰 따로 조정할 필요가 없다** — 조정해야
했다면 "분할 전 공시된 배당금을 분할 후 기준으로 고치는" 별도 규칙이 필요했을 것이고,
그건 새로운 오류의 온상이 됐을 것이다.

세금 — 세전과 세후를 둘 다 낸다
-------------------------------
한국 배당소득세는 **15.4%** 다(소득세 14% + 지방소득세 1.4%, 원천징수).

세전(gross)을 기본으로 삼는 이유는 세율이 투자자마다 다르기 때문이다 — 개인·법인·
외국인·연금계좌가 전부 다르고, 금융소득종합과세 대상이면 15.4% 가 아니다. 그리고
비교 대상인 공식 지수(KRX 의 TR 지수)가 세전이라, 세후로 만들면 벤치마크와 나란히
놓을 수 없다.

그래도 세후를 함께 내는 이유는 **사용자가 실제로 쓸 수 있어야** 하기 때문이다. 개인이
직접 굴리는 계좌라면 손에 들어오는 것은 세후다. 두 칸을 나란히 두면 "세금이 성과를
얼마나 깎는가" 가 곧바로 보인다.

무엇을 하지 않는가
------------------
- **배당금 지급 시차를 반영하지 않는다.** 배당락일에 곧바로 재투자한 것으로 친다.
- **거래비용·세금 외 수수료를 넣지 않는다.** 재투자 매수에도 수수료가 든다.
- **우선주 배당(dps_pref)을 쓰지 않는다.** 공시가 보통주 종목코드로만 와서 우선주
  종목코드에 붙일 수가 없다(#33 열린 질문). 우선주 종목의 TR 은 **배당이 0인 채로**
  계산된다 — 즉 그 종목들의 TR 은 PR 과 같고, 실제보다 낮다.
- **상장 전·거래정지로 그날 시세가 없는 배당은 적용하지 않는다.** 조용히 버리지 않고
  ``skipped_no_price`` 로 세어 보고한다.
"""

from __future__ import annotations

import argparse
import sqlite3
from typing import Dict, Iterable, List, Optional

from collector import db

#: 배당소득세율. 소득세 14% + 지방소득세 1.4% = 15.4% (원천징수)
DIVIDEND_TAX_RATE = 0.154

#: 계수가 이보다 크면 데이터 이상을 의심한다.
#:
#: 배당수익률 30% 는 현실에 있다(청산 배당·특별배당). 그러나 100% 를 넘으면 주가보다
#: 배당금이 크다는 뜻이라, 공시 파싱이 틀렸거나 그 날 종가가 이상한 것이다.
#: **자동으로 고치지 않고 표시만 한다** — preprocess 와 같은 원칙이다.
DIV_FACTOR_SANITY = 2.0


def dividends_by_exdate(conn: sqlite3.Connection, code: str) -> Dict[str, float]:
    """``배당락일 → 주당 배당금(원)``. 한 종목분.

    ★ 왜 단순 ``SUM`` 이 아닌가 — 정정공시가 **기준일을 바꿔** 올라오면 원본과 정정본이
    두 행으로 남는다. ``dividend`` 의 기본키가 (종목, 기준일)이기 때문이다. 실측 2건::

        대신증권 003540  기준일 20211231 접수 20220228800680  주당 1,400원
                         기준일 20220101 접수 20220228801106  주당 1,400원  ← [기재정정]
                         → 배당락일은 둘 다 20211229, 금액도 같다

    합치면 2,800원이 된다. **배당을 두 번 준 셈**이고, 백테스트가 조용히 틀린다.
    그래서 같은 배당락일에 여러 건이면 **접수번호가 가장 큰 것 하나만** 쓴다 —
    ``sources/dart.py`` 의 upsert 규칙("나중 접수번호가 이긴다")과 같은 규칙이다.

    ⚠️ 한 종목이 같은 날 **서로 다른 배당**을 두 건 받는 경우(예: 결산배당과 특별배당이
    같은 기준일)는 이 규칙이 하나를 버린다. 실측에서는 그런 짝이 없었다 — 중복 2건은
    둘 다 정정공시 짝이었다. 새로 생기면 ``report_nm`` 이 다를 것이므로 거기서 보인다.
    """
    out: Dict[str, float] = {}
    # ORDER BY rcept_no 로 오름차순 → dict 에 덮어쓰면 **가장 큰 접수번호가 남는다**
    for r in conn.execute(
            "SELECT ex_div_dt, dps FROM dividend "
            "WHERE srtn_cd=? AND ex_div_dt<>'' AND dps IS NOT NULL AND dps>0 "
            "ORDER BY ex_div_dt, rcept_no", (code,)):
        out[r["ex_div_dt"]] = r["dps"]
    return out


def _series(conn: sqlite3.Connection, code: str) -> List[sqlite3.Row]:
    """원본 종가와 조정 종가를 한 줄로 붙여 읽는다.

    ``price_adjusted`` 를 LEFT JOIN 하는 이유: preprocess 를 아직 안 돌렸거나 일부
    종목만 돌린 상태에서도 이 함수가 죽지 않아야 한다. 조정값이 없으면 그 종목은
    건너뛴다(아래 ``build``).
    """
    return list(conn.execute(
        "SELECT p.bas_dt, p.clpr, a.adj_clpr "
        "FROM price_daily p LEFT JOIN price_adjusted a "
        "  ON a.bas_dt=p.bas_dt AND a.srtn_cd=p.srtn_cd "
        "WHERE p.srtn_cd=? ORDER BY p.bas_dt", (code,)))


def build(conn: sqlite3.Connection, codes: Optional[Iterable[str]] = None,
          *, tax_rate: float = DIVIDEND_TAX_RATE, verbose: bool = False) -> Dict[str, int]:
    """TR 계열을 다시 만든다. **몇 번을 돌려도 같은 결과**가 된다.

    지수는 각 종목의 **첫 거래일을 1.0** 으로 둔다. 종목마다 시작일이 달라도 되는 이유는,
    쓰는 쪽이 보는 것이 지수의 절대값이 아니라 **두 날 사이의 비율**이기 때문이다.

    ★ ``price_adjusted`` 와 방향이 반대라는 점에 주의한다. 수정주가는 **뒤에서 앞으로**
    누적해 오늘을 1.0 으로 맞춘다(과거를 고치는 일이므로 오늘 가격이 바뀌면 안 된다).
    TR 지수는 **앞에서 뒤로** 누적한다 — 누적 수익이 얼마인지가 곧 답이라서, 시작점이
    고정돼야 읽을 수 있다. 어제 본 화면과 오늘 본 화면에서 과거 구간이 달라지지 않는다는
    점도 같다.
    """
    if codes is None:
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT srtn_cd FROM price_daily ORDER BY srtn_cd")]
    codes = list(codes)

    tally = {"codes": 0, "rows": 0, "no_adjusted": 0,
             "div_applied": 0, "skipped_no_price": 0, "skipped_no_close": 0,
             "suspect": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for code in codes:
            rows = _series(conn, code)
            if not rows:
                continue
            if all(r["adj_clpr"] is None for r in rows):
                # 수정주가가 아직 없다. 추측해서 원본 종가로 대신하지 않는다 —
                # 그러면 분할일에 가짜 폭락이 TR 에 그대로 들어간다.
                tally["no_adjusted"] += 1
                continue

            divs = dividends_by_exdate(conn, code)
            traded = {r["bas_dt"] for r in rows}
            for d in divs:
                if d not in traded:
                    # 상장 전 배당이거나 그날 시세가 없다. 조용히 버리지 않는다.
                    tally["skipped_no_price"] += 1

            out = []
            tr = trn = pr = 1.0
            prev_adj: Optional[float] = None
            for r in rows:
                adj, clpr = r["adj_clpr"], r["clpr"]
                # 가격 수익 — 조정 종가의 비율. 분할·권리락은 여기서 이미 고쳐져 있다.
                px = 1.0
                if prev_adj and adj:
                    px = adj / prev_adj

                dps = divs.get(r["bas_dt"])
                f_gross = f_net = 1.0
                if dps:
                    if clpr and clpr > 0:
                        # ★ 같은 날의 두 값을 나누므로 분할 조정과 무관하다 (머리말 참고)
                        f_gross = 1.0 + dps / clpr
                        f_net = 1.0 + dps * (1.0 - tax_rate) / clpr
                        tally["div_applied"] += 1
                        if f_gross > DIV_FACTOR_SANITY:
                            tally["suspect"] += 1
                            if verbose:
                                print(f"  ⚠️ 배당 계수가 상식 밖이다 — {code} {r['bas_dt']} "
                                      f"주당 {dps:,.0f}원 / 종가 {clpr:,}원 = "
                                      f"{f_gross:.3f} (사람이 확인해야 한다)")
                    else:
                        # 거래정지 등으로 종가가 0·NULL 이면 재투자 가격을 정할 수 없다.
                        tally["skipped_no_close"] += 1

                pr *= px
                tr *= px * f_gross
                trn *= px * f_net
                out.append((r["bas_dt"], code, tr, trn, pr, f_gross,
                            dps if (dps and clpr and clpr > 0) else None))
                if adj:
                    prev_adj = adj

            conn.execute("DELETE FROM price_total_return WHERE srtn_cd=?", (code,))
            conn.executemany(
                "INSERT INTO price_total_return "
                "(bas_dt,srtn_cd,tr_index,tr_index_net,pr_index,div_factor,dps_applied) "
                "VALUES (?,?,?,?,?,?,?)", out)
            tally["codes"] += 1
            tally["rows"] += len(out)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return tally


# ==================================================
# 보고 — 숫자를 사람이 읽을 수 있게
# ==================================================
def summary(conn: sqlite3.Connection, code: str) -> Optional[Dict]:
    """한 종목의 TR·PR 누적 수익과 배당 기여."""
    row = conn.execute(
        "SELECT bas_dt, tr_index, tr_index_net, pr_index FROM price_total_return "
        "WHERE srtn_cd=? ORDER BY bas_dt DESC LIMIT 1", (code,)).fetchone()
    if not row:
        return None
    first = conn.execute(
        "SELECT bas_dt FROM price_total_return WHERE srtn_cd=? ORDER BY bas_dt LIMIT 1",
        (code,)).fetchone()
    nm = conn.execute(
        "SELECT itms_nm FROM price_daily WHERE srtn_cd=? ORDER BY bas_dt DESC LIMIT 1",
        (code,)).fetchone()
    ndiv = conn.execute(
        "SELECT COUNT(*) FROM price_total_return WHERE srtn_cd=? AND dps_applied IS NOT NULL",
        (code,)).fetchone()[0]
    return {
        "code": code, "name": nm["itms_nm"] if nm else "",
        "from": first["bas_dt"], "to": row["bas_dt"],
        "tr": row["tr_index"], "tr_net": row["tr_index_net"], "pr": row["pr_index"],
        "divs": ndiv,
    }


def _print_summary(s: Dict) -> None:
    pr, tr, trn = s["pr"], s["tr"], s["tr_net"]
    print(f"  {s['code']} {s['name']}  {s['from']} ~ {s['to']}  (배당 {s['divs']}회)")
    print(f"    PR(가격만)      {pr:8.4f}  →  {(pr - 1) * 100:+7.2f}%")
    print(f"    TR(세전)        {tr:8.4f}  →  {(tr - 1) * 100:+7.2f}%"
          f"   배당 기여 {(tr / pr - 1) * 100:+6.2f}%p")
    print(f"    TR(세후 15.4%)  {trn:8.4f}  →  {(trn - 1) * 100:+7.2f}%"
          f"   배당 기여 {(trn / pr - 1) * 100:+6.2f}%p")


def status(conn: sqlite3.Connection) -> None:
    """적재 현황."""
    n, s, a, b = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT srtn_cd), MIN(bas_dt), MAX(bas_dt) "
        "FROM price_total_return").fetchone()
    print("― TR 계열 현황 ―")
    if not n:
        print("  아직 없다. `python -m collector.total_return build` 로 만든다.")
        return
    print(f"  {n:,}행 · 종목 {s:,}개 · {a} ~ {b}")
    nd = conn.execute(
        "SELECT COUNT(*) FROM price_total_return WHERE dps_applied IS NOT NULL").fetchone()[0]
    print(f"  배당이 적용된 날 {nd:,}건")
    print("\n  TR 이 PR 보다 많이 오른 종목 (배당 기여 상위 10)")
    for r in conn.execute("""
        SELECT t.srtn_cd, t.tr_index, t.pr_index,
               (SELECT itms_nm FROM price_daily p WHERE p.srtn_cd=t.srtn_cd
                 ORDER BY bas_dt DESC LIMIT 1) nm
        FROM price_total_return t
        WHERE t.bas_dt=(SELECT MAX(bas_dt) FROM price_total_return WHERE srtn_cd=t.srtn_cd)
          AND t.pr_index>0 AND t.tr_index/t.pr_index > 1.0
        ORDER BY t.tr_index/t.pr_index DESC LIMIT 10"""):
        print(f"    {r['srtn_cd']} {r['nm'][:14]:<14} "
              f"배당 기여 {(r['tr_index'] / r['pr_index'] - 1) * 100:+7.2f}%p")


def verify(conn: sqlite3.Connection, codes: List[str]) -> None:
    """검증 — TR 과 PR 의 차이가 배당수익률 누적과 맞는가.

    맞아떨어져야 하는 관계는 이것이다::

        TR ÷ PR  =  ∏ (1 + DPS_i ÷ 그날 종가)

    왼쪽은 ``build`` 가 누적하며 만든 값이고, 오른쪽은 ``dividend`` 표에서 곧바로 다시
    곱한 값이다. **서로 다른 경로로 같은 수에 도달해야** 한다. 어긋나면 누적 과정에
    새는 곳이 있다는 뜻이다.
    """
    print("― 검증: TR ÷ PR 이 배당 계수의 곱과 같은가 ―")
    for code in codes:
        s = summary(conn, code)
        if not s:
            print(f"  {code}: TR 계열이 없다")
            continue
        _print_summary(s)
        # 독립 경로 — 표에 기록된 dps_applied 와 그날 원본 종가로 다시 곱한다
        prod = 1.0
        for r in conn.execute(
                "SELECT t.dps_applied, p.clpr FROM price_total_return t "
                "JOIN price_daily p ON p.bas_dt=t.bas_dt AND p.srtn_cd=t.srtn_cd "
                "WHERE t.srtn_cd=? AND t.dps_applied IS NOT NULL ORDER BY t.bas_dt", (code,)):
            prod *= 1.0 + r["dps_applied"] / r["clpr"]
        got = s["tr"] / s["pr"]
        ok = abs(got - prod) < 1e-9
        print(f"    누적 TR÷PR {got:.10f} vs 배당계수 곱 {prod:.10f} "
              f"→ {'✅ 일치' if ok else '❌ 어긋남'}")
        print()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m collector.total_return",
        description="총수익(TR) 계열 — 수정주가에 배당을 더한다")
    p.add_argument("mode", choices=["build", "status", "verify"],
                   help="build=다시 계산 · status=현황 · verify=TR/PR 대조")
    p.add_argument("--codes", help="대상 종목코드 (예: 005930,000660). 생략하면 전 종목")
    p.add_argument("--tax", type=float, default=DIVIDEND_TAX_RATE * 100,
                   help="세후 계열에 쓸 배당소득세율(%%). 기본 15.4")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)

    conn = db.connect()
    codes = [c.strip() for c in a.codes.split(",")] if a.codes else None
    try:
        if a.mode == "status":
            status(conn)
        elif a.mode == "verify":
            verify(conn, codes or ["005930", "000660", "005380", "035720"])
        else:
            print("TR 계열 계산 중 …")
            t = build(conn, codes, tax_rate=a.tax / 100.0, verbose=not a.quiet)
            print(f"  종목 {t['codes']:,} · 행 {t['rows']:,} · 배당 적용 {t['div_applied']:,}건")
            if t["no_adjusted"]:
                print(f"  ⚠️ 수정주가가 없어 건너뛴 종목 {t['no_adjusted']:,} "
                      f"— `python -m collector.preprocess` 를 먼저 돌린다")
            if t["skipped_no_price"]:
                print(f"  ⚠️ 그날 시세가 없어 적용 못 한 배당 {t['skipped_no_price']:,}건 "
                      f"(상장 전이거나 거래가 없던 날)")
            if t["skipped_no_close"]:
                print(f"  ⚠️ 종가가 없어 재투자 가격을 못 정한 배당 {t['skipped_no_close']:,}건")
            if t["suspect"]:
                print(f"  ⚠️ 배당 계수가 상식 밖인 건 {t['suspect']:,}건 (표시만 했다)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
