"""전자공시(DART) — 배당 공시.

무엇을 메우려고 만들었나
------------------------
지금까지 만든 수정주가는 분할·권리락만 반영한 **PR(가격수익)** 이다. 배당이 빠져 있다.

> **용어 — PR 과 TR**
> **PR(Price Return, 가격수익)** — 주가 변동만으로 계산한 수익률.
> **TR(Total Return, 총수익)** — 주가 변동 **+ 받은 배당**까지 합한 수익률.
> 헷갈리는 점 — 현금배당은 거래소가 기준가를 **조정하지 않는다.** 그래서 포털 응답의
> ``vs``(전일대비)에도 나타나지 않고, 우리 ``corporate_action`` 표에도 잡히지 않는다.
> 배당은 가격을 고쳐서 넣는 게 아니라 **수익률에 더하는 별도 항목**이다.

배당을 빼놓으면 무엇이 틀리나: 배당수익률이 높은 종목(은행·통신·지주)을 고르는 전략은
성과가 **체계적으로 낮게** 나온다. 벤치마크와의 비교도 성립하지 않는다 — KOSPI TR 과
우리 PR 을 견주면 매년 배당수익률만큼 지는 것이 당연하다.

왜 공시 본문인가 — 경로 둘을 재 보고 골랐다 (#33 10.4)
-------------------------------------------------------
``alotMatter.json`` (사업보고서 배당 항목)  주당 배당금은 나오지만 **① 배당기준일이
없고 ② 연간 합계다.** 분기배당 회사는 언제 얼마를 받았는지 알 수 없다. → 교차검증용.

``document.xml`` (「현금ㆍ현물배당결정」 공시 본문)  **배당기준일이 정확히 나온다.**
응답이 2KB 로 가볍고 표 형식이 고정돼 있다. → 이쪽을 주 경로로 쓴다.

왜 전 기간이 아니라 배당철만 훑나
---------------------------------
``list.json`` 은 회사를 안 지정하고 **기간+공시유형**으로 전체 조회가 된다. 다만 기간이
3개월을 넘으면 거부되고(``status=100``), 한 달 수시공시가 5,000건을 넘어 100건씩 50여
페이지다. 전 기간(6년 9개월)을 훑으면 목록·본문 합쳐 약 39,000회 — 하루 한도 20,000 을
넘겨 **이틀**이 걸린다.

배당 결의는 달이 정해져 있다: 결산배당은 1~3월, 분기·중간배당은 4·7·10월, 이사회를 미리
여는 곳이 12월. 그 달만 훑으면 **하루 안에** 끝난다. 어느 달을 훑었는지는 ``ingest_day``
에 남으므로, 나중에 달을 늘리면 **안 훑은 달만** 추가로 받는다.

배당락일을 어떻게 구하나 — ★ "T-2" 가 아니다
---------------------------------------------
TR 에 배당을 더해야 하는 날은 기준일이 아니라 **배당락일**이다. 그런데 기준일에서
달력으로 이틀을 빼면 틀린다. 결제는 **거래일** 기준이고, 기준일이 휴장일일 수도 있다.

    L = 기준일 이하의 마지막 거래일
    배당락일 = L 의 한 거래일 전

    왜: 매수일 D 의 결제일은 D+2거래일이다. 기준일에 주주명부에 있으려면
        D+2거래일 ≤ L 이어야 하므로 배당받는 마지막 매수일은 L-2거래일이고,
        그 다음 날(= L-1거래일)부터 배당이 없다. 그날이 배당락일이다.

    실측 대조 (KRX 공표값):
        기준일 2020-12-31(휴장) → L=12-30(2020년 마지막 거래일) → 배당락 12-29 ✅
        달력으로 T-2 를 하면 12-29 가 나와 **우연히 맞는다.**
        기준일 2021-06-30(거래일) → L=06-30 → 배당락 06-29
        달력으로 T-2 를 하면 06-28 이 나와 **틀린다.** ← 그래서 거래일로 센다

거래일 달력은 이미 있다 — ``price_daily`` 에 쌓인 1,648 거래일이 그것이다. 틀린 달력을
따로 들고 있지 않으려는 것은 ``portal.weekdays`` 와 같은 태도다.

⚠️ 2024년부터 배당기준일을 배당액 확정 후로 미룰 수 있게 제도가 바뀌었다. 그래도 이
   공식은 깨지지 않는다 — 공식이 읽는 것은 **공시에 적힌 기준일**이고, 기준일이 3월이든
   12월이든 결제 규칙은 같다. 다만 배당락 시점의 **가격 반응**은 달라질 수 있다(금액을
   이미 알고 있으므로). 미검증이라 🟡 로 둔다.
"""

from __future__ import annotations

import bisect
import io
import json
import re
import sqlite3
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from collector import config, raw_store
from collector.ratelimit import RateLimiter

#: 배당 결의가 몰리는 달. 이 달만 목록을 훑는다.
#:
#: 1~3월 결산배당 · 4·7·10월 분기/중간배당 · 12월 이사회를 미리 여는 곳.
#: 늘려도 손해가 없다(이미 훑은 달은 건너뛴다). 줄이면 그 달 배당을 통째로 놓친다.
DIVIDEND_MONTHS = (1, 2, 3, 4, 7, 10, 12)

#: 목록에서 배당 공시를 골라내는 조건. 제목에 이 글자가 있으면 본문을 받는다.
#:
#: ⚠️ 정확한 제목으로 매칭하지 않는다. 실측한 제목은 「현금ㆍ현물배당결정」 ·
#:    「[기재정정]현금ㆍ현물배당결정」 · 「현금ㆍ현물배당결정(자회사의 주요경영사항)」
#:    처럼 갈리고, 가운뎃점이 `ㆍ`(U+318D) 라 눈으로는 구분이 안 된다. 넓게 잡고
#:    **아래 제외 규칙으로 좁히는** 쪽이 안전하다 — 놓친 배당은 영영 모르지만, 잘못
#:    받은 공시는 파싱에 실패해 `needs_review` 로 남아 눈에 띈다.
DIVIDEND_KEYWORD = "배당"

#: 제목에 이것이 있으면 **본문을 받지 않는다.** 표본 조사(2020~2025 · 12건)에서 찾은
#: 오검출 두 종류다. 목록 단계에서 걸러 호출을 아낀다.
#:
#: ``자회사``·``종속회사``  「현금ㆍ현물배당결정(자회사의 주요경영사항)」 은 **자회사의**
#:   배당을 모회사가 신고한 것이다. 본문에 적힌 배당금은 자회사 것이므로, 공시를 낸
#:   종목코드에 붙이면 **두 종목이 함께 틀린다.** 실측: 에코프로(086520)가 낸 공시의
#:   450원은 에코프로비엠(247540) 배당이었다. 자회사가 상장사면 **자기 공시를 따로
#:   내므로** 걸러도 잃는 것이 없다.
#:
#: ``주주명부폐쇄``  「현금ㆍ현물배당을위한주주명부폐쇄(기준일)결정」 은 기준일만 정하는
#:   공시로 **배당금이 안 적혀 있다.** 금액은 나중에 「현금ㆍ현물배당결정」 으로 나온다.
#:   🟡 기준일 교차검증에는 쓸 수 있어, 버리지 말고 세어서 보고만 한다.
#:
#: ``주식배당``  「주식배당결정」 은 **현금이 아니라 주식**을 준다. 이 표에 넣으면 안 되는
#:   이유가 둘이다. ① 서식이 달라 "1주당 배당금(원)" 칸이 없다(실측 24건 전부 금액 미검출).
#:   ② **거래소가 배당락일에 기준가를 조정한다** — 주식수가 늘기 때문이다. 그래서 이미
#:   `vs` 를 통해 `corporate_action` 표에 잡혀 있고, 여기에 또 담으면 **이중 계상**이다.
#:   🟢 실측으로 확인했다: 기준일 2020-12-31 주식배당 공시 22종목이 **22/22 전부**
#:      우리가 역산한 배당락일 `20201229` 에 `corporate_action` 에 들어 있었다.
DIVIDEND_EXCLUDE = ("자회사", "종속회사", "주주명부폐쇄", "주식배당")

#: 본문에서 **정정신고 블록이 끝나고 실제 서식이 시작되는 자리**를 찾는 표시.
#:
#: ⚠️ 정정공시 본문은 맨 앞에 `정정항목 | 정정전 | 정정후` 표가 붙는다. 위에서부터
#:    라벨을 찾으면 **정정전 값을 집을 수 있다.** 그래서 이 표시의 **마지막** 출현
#:    위치부터 파싱한다 — 정정 블록은 항상 서식보다 앞에 오기 때문이다.
#: ⚠️ 줄 **머리**로 찾으면 안 된다. 서식 제목이 표 밖(``<div>``)에 있어 `</tr>` 로
#:    끊기지 않고 첫 행과 **한 줄로 붙는다** — 실측: `한국팩키지/… 결정 1. 배당구분`.
#:    그래서 줄 안 어디든 찾는다.
_FORM_START = re.compile(r"1\s*\.\s*배당구분")

#: 본문에 이것이 있으면 자회사 대신 신고한 공시다. 제목 제외를 빠져나온 것까지 잡는
#: 이중 안전장치 — 종목을 틀리게 붙이는 것이 여기서 가장 비싼 실수다.
_SUBSIDIARY_BODY = re.compile(r"자회사인|종속회사인|자회사\s*의\s*주요경영사항")

#: DART 가 쓰는 상태 코드 중 우리가 구분해야 하는 것들.
DART_OK = "000"
DART_NO_DATA = "013"          # 조회된 데이터 없음 — 오류가 아니다
DART_NO_FILE = "014"          # 파일이 존재하지 않습니다 — 목록엔 있는데 본문을 못 준다
DART_QUOTA_EXCEEDED = "020"   # 하루 한도 초과


class DartError(RuntimeError):
    """DART 가 정상 응답을 주지 않았다."""


class DartQuotaExceeded(RuntimeError):
    """DART 하루 한도를 넘겼다. 오늘은 더 부르지 않는다."""


class DartNoDocument(RuntimeError):
    """목록에는 있는데 본문 파일을 받을 수 없다 (``status=014``).

    실측: 2020-01 공시 일부가 이렇게 온다. 이유는 알 수 없고 우리가 고칠 수도 없다.
    **한 건 때문에 46달 훑기를 멈추면 안 되므로** 별도 예외로 두고, 부르는 쪽이
    건너뛰며 센다. 세지 않고 조용히 넘기면 "그 달엔 배당이 없었다" 로 둔갑한다.
    """


@dataclass
class Dividend:
    """배당 공시 한 건에서 읽어낸 것."""
    srtn_cd: str = ""
    corp_code: str = ""
    itms_nm: str = ""
    rcept_no: str = ""
    report_nm: str = ""
    record_dt: str = ""        # 배당기준일 YYYYMMDD
    ex_div_dt: str = ""        # 배당락일 (거래일 달력으로 역산)
    board_dt: str = ""         # 이사회결의일
    div_kind: str = ""         # 결산배당 | 분기배당 | 중간배당
    div_type: str = ""         # 현금배당 | 현물배당 | 주식배당
    dps: Optional[float] = None        # 1주당 배당금(원) 보통주식
    dps_pref: Optional[float] = None   # 1주당 배당금(원) 종류주식(우선주)
    yield_pct: Optional[float] = None  # 시가배당율(%)
    total_amt: Optional[int] = None    # 배당금총액(원)
    raw_sha256: str = ""
    needs_review: int = 0
    note: str = ""


@dataclass
class ScanResult:
    """한 달을 훑은 결과."""
    ym: str
    list_calls: int = 0
    total_reports: int = 0        # 그 달 수시공시 전체 건수
    dividend_reports: int = 0     # 본문을 받을 대상
    rows: List[Dict] = field(default_factory=list)
    excluded: List[Dict] = field(default_factory=list)  # 제목에 '배당'은 있으나 제외된 것
    #: 제외 사유별 건수. "무엇을 왜 버렸나" 를 보고에 남기려고 센다.
    excluded_by: Dict[str, int] = field(default_factory=dict)


def exclude_reason(report_nm: str) -> str:
    """제목만 보고 제외 사유를 돌려준다. 받아야 할 공시면 빈 문자열.

    사유를 **이름으로** 돌려주는 이유: 제외 건수만 세면 "무엇을 왜 버렸나" 가 사라진다.
    특히 ``주식배당`` 은 버리는 게 아니라 **다른 표(`corporate_action`)가 이미 갖고 있는
    것**이라, 그 구분이 보고에 남아야 한다.
    """
    if DIVIDEND_KEYWORD not in report_nm:
        return "무관"
    for x in DIVIDEND_EXCLUDE:
        if x in report_nm:
            return x
    return ""


def is_dividend_report(report_nm: str) -> bool:
    """제목만 보고 **본문을 받을지** 정한다. 제외 근거는 ``DIVIDEND_EXCLUDE`` 주석."""
    return exclude_reason(report_nm) == ""


# ==================================================
# 1. 호출 — 한 군데로 모은다
# ==================================================
def _get(limiter: RateLimiter, url: str, params: Dict, *,
         session: Optional[requests.Session] = None, timeout: int = 60) -> requests.Response:
    """간격을 지켜 한 번 부른다.

    ``limiter.check_budget()`` 은 부르지 않는다 — DART 는 남은 유량을 헤더로 주지
    않으므로 그 함수가 아무 일도 하지 않는다(config.DART_SLEEP 주석). 한도는 부르는
    쪽이 ``limiter.calls`` 를 세서 지킨다.
    """
    limiter.wait()
    sess = session or requests
    r = sess.get(url, params={**params, "crtfc_key": config.dart_key()}, timeout=timeout)
    limiter.observe(r.headers)   # DART 는 안 주지만, 언젠가 주면 공짜로 받는다
    return r


def _check_status(status: str, message: str, where: str) -> None:
    """DART 상태 코드를 판정한다. 한도 초과만 별도 예외다 — **재시도해선 안 되므로.**"""
    if status == DART_OK or status == DART_NO_DATA:
        return
    if status == DART_NO_FILE:
        raise DartNoDocument(f"본문 파일이 없다 ({where}): status={status} {message!r}")
    if status == DART_QUOTA_EXCEEDED:
        raise DartQuotaExceeded(
            f"DART 하루 한도를 넘겼다 ({where}): status={status} {message!r}\n"
            "  왜 멈추나: 더 부르면 전부 같은 오류로 돌아온다. 유량만 태운다.\n"
            "  할 일: 내일 다시 돌린다. 어느 달까지 훑었는지는 ingest_day 에 남아 있어\n"
            "         멈춘 자리에서 이어서 간다."
        )
    raise DartError(
        f"DART 가 정상 응답이 아니다 ({where}): status={status} {message!r}\n"
        "  할 일: status=010/011 이면 DART_API_KEY 를 확인한다(오픈API 이용현황에서\n"
        "         키 상태를 본다). status=100 이면 요청 값이 잘못됐다 — 기간이 3개월을\n"
        "         넘지 않는지 먼저 본다. status=800 은 시스템 점검이라 기다리면 된다."
    )


def fetch_list(limiter: RateLimiter, *, bgn_de: str, end_de: str, page_no: int = 1,
               page_count: int = 100, pblntf_detail_ty: str = "I001",
               session: Optional[requests.Session] = None) -> Tuple[int, int, List[Dict]]:
    """공시 목록 한 페이지. ``(total_count, total_page, 목록)``.

    ``pblntf_detail_ty='I001'`` 은 거래소 **수시공시**다. 배당결정 공시가 여기 들어온다.
    상위 분류 ``pblntf_ty='I'``(거래소공시 전체)보다 한 달 건수가 적어 페이지가 준다.

    ⚠️ ``bgn_de``~``end_de`` 가 3개월을 넘으면 ``status=100`` 으로 거부된다(실측 #33).
       그래서 이 수집기는 **달 단위로만** 부른다.
    """
    r = _get(limiter, config.DART_LIST_URL, {
        "bgn_de": bgn_de, "end_de": end_de, "page_no": page_no,
        "page_count": page_count, "pblntf_detail_ty": pblntf_detail_ty,
    }, session=session)
    if r.status_code != 200:
        raise DartError(f"목록 조회 HTTP {r.status_code} ({bgn_de}~{end_de} p{page_no})")
    doc = r.json()
    status = str(doc.get("status", ""))
    _check_status(status, str(doc.get("message", "")), f"목록 {bgn_de}~{end_de} p{page_no}")
    if status == DART_NO_DATA:
        return 0, 0, []
    return (int(doc.get("total_count") or 0), int(doc.get("total_page") or 0),
            doc.get("list") or [])


def fetch_document(conn: sqlite3.Connection, limiter: RateLimiter, rcept_no: str, *,
                   session: Optional[requests.Session] = None,
                   keep_raw: bool = True, reuse_raw: bool = True) -> bytes:
    """공시 본문 XML 바이트. 원문(zip)은 ``raw_response`` 에 남긴다.

    ``reuse_raw=True`` 면 **이미 받아 둔 원문이 있으면 네트워크를 안 탄다.** 파싱 규칙을
    고쳐 다시 돌릴 때가 반드시 오는데, 그때 수천 건을 다시 받으면 하루 한도를 통째로
    쓴다. 재개·재파싱의 근거를 ``ingest_day`` 가 아니라 원문 자체에 두는 이유다.

    ⚠️ 응답은 **zip** 이다. 오류일 때만 XML 로 온다 — 그래서 zip 서명(`PK`)을 먼저 본다.
       XML 인 줄 알고 파싱하면 오류 메시지가 조용히 "본문 0건" 으로 둔갑한다.
    """
    target = f"dividend/{rcept_no}"
    if reuse_raw:
        kept = raw_store.load(conn, "dart", target)
        if kept:
            return _unzip(kept["body"], rcept_no)

    r = _get(limiter, config.DART_DOC_URL, {"rcept_no": rcept_no}, session=session)
    if r.status_code != 200:
        raise DartError(f"본문 조회 HTTP {r.status_code} (rcept_no={rcept_no})")
    body = r.content

    if not body.startswith(b"PK"):
        # zip 이 아니면 오류 XML 이다. 상태 코드를 꺼내 판정한다.
        text = body.decode("utf-8", "replace")
        m = re.search(r"<status>(\d+)</status>", text)
        msg = re.search(r"<message>(.*?)</message>", text, re.S)
        _check_status(m.group(1) if m else "?", msg.group(1).strip() if msg else text[:200],
                      f"본문 {rcept_no}")
        raise DartError(f"본문이 zip 이 아니다 (rcept_no={rcept_no}): {text[:200]!r}")

    if keep_raw:
        conn.execute("BEGIN IMMEDIATE")
        try:
            raw_store.save(conn, "dart", target, body,
                           http_status=r.status_code, note=f"bytes={len(body)}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return _unzip(body, rcept_no)


def _unzip(body: bytes, rcept_no: str) -> bytes:
    """zip 안의 첫 XML 을 꺼낸다. 여러 개면 **가장 큰 것** — 본문이 첨부보다 크다."""
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if not names:
            raise DartError(f"본문 zip 이 비어 있다 (rcept_no={rcept_no})")
        names.sort(key=lambda n: zf.getinfo(n).file_size, reverse=True)
        return zf.read(names[0])


# ==================================================
# 2. 본문 파싱
# ==================================================
#: 태그를 지우고 표를 줄·칸으로 눕히기 위한 치환. **순서가 중요하다** — flatten 참고.
#:
#: ``TE``·``TU`` 까지 넣어 둔 것은 공시 본문이 HTML(``td``)로 오는 서식과 KRX XML
#: (``TE``)로 오는 서식이 섞여 있어서다. 실측한 배당 공시는 HTML 이었다.
_CELL_END = re.compile(r"</\s*(?:TD|TE|TH|TU)\s*>", re.I)
_ROW_END = re.compile(r"</\s*TR\s*>", re.I)
_TAG = re.compile(r"<[^>]*>", re.S)
_ANY_WS = re.compile(r"\s+")
#: ``<style>``·``<script>`` 안쪽은 표가 아니다. **태그만 벗기면 CSS 글자가 표 칸으로
#: 섞여 들어온다** — 실측한 공시 본문은 8KB 중 6KB 가 CSS 였다.
_DROP_BLOCK = re.compile(r"<\s*(style|script)\b[^>]*>.*?</\s*\1\s*>", re.I | re.S)


def decode_document(xml: bytes) -> str:
    """공시 본문을 문자열로. **선언에 적힌 인코딩을 따른다.**

    ⚠️ 배당 공시 본문은 **euc-kr** 로 온다(실측 #33). utf-8 로 읽으면 한글 라벨이 깨져
       정규식이 하나도 안 맞고, 그러면 "배당 공시가 없었다" 로 조용히 둔갑한다.
       그래서 추측하지 않고 선언을 읽는다.
    """
    head = xml[:200].decode("ascii", "replace").lower()
    m = re.search(r'encoding=["\']([\w-]+)["\']', head)
    enc = (m.group(1) if m else "euc-kr")
    for cand in (enc, "euc-kr", "cp949", "utf-8"):
        try:
            return xml.decode(cand)
        except (UnicodeDecodeError, LookupError):
            continue
    return xml.decode("euc-kr", "replace")


def flatten(xml: bytes) -> List[str]:
    """공시 본문을 **표의 한 줄 = 문자열 한 줄**로 눕힌다. 칸은 탭으로 나눈다.

    왜 XML 트리로 파싱하지 않나: 공시 본문의 표 구조가 연도·시장·서식 버전마다 다르고
    (``<table>``·``<TE>``·``rowspan``), 트리 경로로 찍으면 한 서식에만 맞는 파서가 된다.
    라벨 글자는 규정 서식이라 잘 안 바뀌므로 **라벨로 찾는** 쪽이 오래 버틴다.

    ⚠️ **원본 줄바꿈을 먼저 없앤다.** 이 순서를 틀리면 파서가 통째로 조용히 실패한다 —
       실제로 한 번 그렇게 만들었다. 공시 본문은 태그마다 줄을 바꿔 놓아서, 줄바꿈을
       살려 둔 채 태그를 벗기면 표 한 줄(``<tr>``)이 여러 줄로 쪼개지고 **라벨과 값이
       서로 다른 줄로 갈린다.** 그러면 라벨은 찾히는데 값 칸이 늘 비어, "공시에 배당이
       안 적혀 있다" 는 잘못된 결론이 나온다.
    """
    text = decode_document(xml)
    text = _DROP_BLOCK.sub(" ", text)
    text = _ANY_WS.sub(" ", text)          # ★ 순서 주의 (위 경고)
    text = _CELL_END.sub("\t", text)
    text = _ROW_END.sub("\n", text)
    text = _TAG.sub("", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    out: List[str] = []
    for line in text.split("\n"):
        # 칸 구분은 살리고 빈 칸은 접는다 — rowspan 때문에 빈 칸이 줄줄이 생긴다.
        cells = [c.strip() for c in line.split("\t")]
        cells = [c for c in cells if c]
        if cells:
            out.append("\t".join(cells))
    return out


def _to_num(s: str) -> Optional[float]:
    """공시 숫자 한 칸. ``-``·``해당없음``·빈 칸은 ``None``."""
    s = (s or "").strip().replace(",", "").replace("원", "").replace("%", "")
    if s in ("", "-", "–", "—", "해당없음", "해당 없음", "미정"):
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def _to_date(s: str) -> str:
    """공시 날짜 한 칸 → ``YYYYMMDD``. 못 읽으면 빈 문자열."""
    m = re.search(r"(20\d{2})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})", s or "")
    if not m:
        return ""
    return f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}"


def _row_value(lines: Sequence[str], i: int, *, want: str = "") -> str:
    """라벨이 있는 줄에서 값 칸을 고른다. ``want`` 가 있으면 그 칸의 **다음** 칸.

    ``rowspan`` 때문에 값이 다음 줄로 넘어가는 경우까지 본다 — 1주당 배당금이
    보통주식/종류주식 두 줄로 쪼개지는 것이 대표적이다.
    """
    cells = lines[i].split("\t")
    if want:
        for j, c in enumerate(cells):
            if want in c and j + 1 < len(cells):
                return cells[j + 1]
        # 같은 줄에 없으면 다음 두 줄까지 (rowspan 으로 라벨만 남은 줄)
        for k in range(i + 1, min(i + 3, len(lines))):
            sub = lines[k].split("\t")
            for j, c in enumerate(sub):
                if want in c and j + 1 < len(sub):
                    return sub[j + 1]
        return ""
    return cells[-1] if len(cells) > 1 else ""


def form_start(lines: Sequence[str]) -> int:
    """실제 서식이 시작되는 줄 번호. 없으면 ``-1``(위 ``_FORM_START`` 참고)."""
    start = -1
    for i, line in enumerate(lines):
        if _FORM_START.search(line):
            start = i          # 마지막 출현을 쓴다
    return start


def is_subsidiary_filing(lines: Sequence[str], start: int) -> bool:
    """자회사 배당을 모회사가 신고한 공시인가.

    ``서식 머리행까지``만 본다. 표시(`자회사인 (주)○○ 의 주요경영사항신고`)가 서식
    제목 줄에 붙어 오기 때문이다 — 실측 에코프로 공시에서 **서식 시작 바로 그 줄**이었다.

    ⚠️ 본문 전체를 뒤지면 안 된다. 「11. 기타 투자판단과 관련한 중요사항」 은 자유
       서술이라 "자회사인 …" 같은 문장이 흔히 들어 있고, 그것까지 잡으면 **멀쩡한 배당을
       버린다.** 여기서는 놓치는 쪽(오귀속)이 더 비싸지만, 그래서 제목 필터
       (`DIVIDEND_EXCLUDE`)와 **이중으로** 막는다.
    """
    upto = lines[:start + 1] if start >= 0 else lines[:8]
    return any(_SUBSIDIARY_BODY.search(ln) for ln in upto)


def parse_document(xml: bytes) -> Dividend:
    """「현금ㆍ현물배당결정」 본문에서 배당 사실을 읽는다.

    못 읽은 칸은 **추측해서 채우지 않는다.** ``needs_review`` 를 세우고 ``note`` 에
    무엇이 없었는지 적는다 — 조용히 0 원으로 담기면 백테스트가 조용히 틀린다.
    """
    all_lines = flatten(xml)
    start = form_start(all_lines)
    d = Dividend()
    missing: List[str] = []
    if is_subsidiary_filing(all_lines, start):
        d.needs_review = 1
        d.note = "자회사 배당을 모회사가 신고한 공시 — 이 종목의 배당이 아니다"
        return d

    lines = all_lines[start:] if start >= 0 else all_lines
    for i, line in enumerate(lines):
        if "배당구분" in line and not d.div_kind:
            d.div_kind = _row_value(lines, i)
        elif "배당종류" in line and not d.div_type:
            d.div_type = _row_value(lines, i)
        elif "주당" in line and "배당금" in line:
            if d.dps is None:
                d.dps = _to_num(_row_value(lines, i, want="보통주"))
            if d.dps_pref is None:
                d.dps_pref = _to_num(_row_value(lines, i, want="종류주"))
        elif "시가배당" in line and d.yield_pct is None:
            d.yield_pct = _to_num(_row_value(lines, i, want="보통주") or _row_value(lines, i))
        elif "배당금총액" in line and d.total_amt is None:
            v = _to_num(_row_value(lines, i))
            d.total_amt = int(v) if v is not None else None
        elif "배당기준일" in line and not d.record_dt:
            d.record_dt = _to_date(_row_value(lines, i)) or _to_date(line)
        elif "이사회결의일" in line and not d.board_dt:
            d.board_dt = _to_date(_row_value(lines, i)) or _to_date(line)

    if not d.record_dt:
        missing.append("배당기준일")
    if d.dps is None and d.dps_pref is None:
        missing.append("1주당 배당금")
    if missing:
        d.needs_review = 1
        d.note = "본문에서 못 읽음: " + ", ".join(missing)
    return d


# ==================================================
# 3. 배당락일 — 거래일 달력으로 역산
# ==================================================
def trading_days(conn: sqlite3.Connection) -> List[str]:
    """쌓인 거래일 달력(YYYYMMDD 오름차순).

    ``price_daily`` 에 자료가 있는 날이 곧 거래일이다. 별도 휴장일 달력을 만들지 않는
    것은 ``portal.weekdays`` 와 같은 이유 — 틀린 달력은 없는 것보다 나쁘다.
    """
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT bas_dt FROM price_daily ORDER BY bas_dt")]


def ex_dividend_date(cal: Sequence[str], record_dt: str) -> Optional[str]:
    """배당기준일 → 배당락일. 달력 밖이면 ``None``.

    모듈 머리말의 공식 그대로다: **기준일 이하 마지막 거래일의 한 거래일 전.**
    """
    if not record_dt or not cal:
        return None
    i = bisect.bisect_right(cal, record_dt) - 1
    if i < 1:
        return None          # 달력 시작 이전 — 우리 구간 밖이다
    if cal[i] == cal[-1]:
        # 기준일이 달력 끝에 걸렸다. 앞은 알지만 이 기준일이 정말 마지막 거래일 이후인지
        # 아직 모른다 — 다음 거래일이 쌓이면 판정이 바뀔 수 있으므로 값은 주고 표시는
        # 부르는 쪽이 한다.
        pass
    return cal[i - 1]


# ==================================================
# 4. 달 단위 훑기
# ==================================================
def month_range(ym: str) -> Tuple[str, str]:
    """``'202102'`` → ``('20210201', '20210228')``."""
    y, m = int(ym[:4]), int(ym[4:6])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    import datetime as _dt
    last = _dt.date(ny, nm, 1) - _dt.timedelta(days=1)
    return f"{y}{m:02d}01", last.strftime("%Y%m%d")


def dividend_months(start_year: int, end_year: int) -> List[str]:
    """훑을 달 목록(``YYYYMM``, 과거→현재)."""
    return [f"{y}{m:02d}" for y in range(start_year, end_year + 1)
            for m in DIVIDEND_MONTHS]


def scan_month(conn: sqlite3.Connection, limiter: RateLimiter, ym: str, *,
               session: Optional[requests.Session] = None,
               max_calls: Optional[int] = None) -> ScanResult:
    """한 달 목록을 끝까지 넘기며 배당 공시만 골라낸다.

    페이지를 **조용히 자르지 않는다.** ``max_calls`` 에 걸려 중간에 멈추면 그 달은
    ``ingest_day`` 에 ``done`` 으로 남기지 않으므로 다음 실행이 처음부터 다시 훑는다.
    """
    bgn, end = month_range(ym)
    res = ScanResult(ym)
    page = 1
    while True:
        if max_calls is not None and limiter.calls >= max_calls:
            raise DartQuotaExceeded(
                f"이번 실행의 호출 상한({max_calls:,}회)에 닿았다 — {ym} 목록 {page}페이지에서 멈췄다.\n"
                "  할 일: 다시 돌리면 이 달부터 이어서 간다(이 달은 아직 done 으로 남기지 않았다)."
            )
        total, total_page, rows = fetch_list(
            limiter, bgn_de=bgn, end_de=end, page_no=page, session=session)
        res.list_calls += 1
        if page == 1:
            res.total_reports = total
        for row in rows:
            why = exclude_reason(row.get("report_nm") or "")
            if why == "":
                res.rows.append(row)
            elif why != "무관":
                # 세어서 보고만 한다 (DIVIDEND_EXCLUDE 참고)
                res.excluded.append(row)
                res.excluded_by[why] = res.excluded_by.get(why, 0) + 1
        if page >= max(total_page, 1):
            break
        page += 1
    res.dividend_reports = len(res.rows)
    return res


# ==================================================
# 5. 저장
# ==================================================
def upsert(conn: sqlite3.Connection, items: Sequence[Dividend]) -> int:
    """배당 표에 쓴다.

    같은 (종목, 기준일)에 **정정공시**가 오면 접수번호가 큰 쪽(나중 공시)이 이긴다.
    그냥 덮어쓰면 처리 순서에 따라 옛 공시가 최종값이 될 수 있어, 조건을 SQL 에 박는다.
    """
    if not items:
        return 0
    conn.executemany(
        "INSERT INTO dividend "
        "(srtn_cd,record_dt,rcept_no,corp_code,itms_nm,report_nm,div_kind,div_type,"
        " dps,dps_pref,yield_pct,total_amt,ex_div_dt,board_dt,raw_sha256,needs_review,note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(srtn_cd,record_dt) DO UPDATE SET "
        "  rcept_no=excluded.rcept_no, corp_code=excluded.corp_code, "
        "  itms_nm=excluded.itms_nm, report_nm=excluded.report_nm, "
        "  div_kind=excluded.div_kind, div_type=excluded.div_type, "
        "  dps=excluded.dps, dps_pref=excluded.dps_pref, "
        "  yield_pct=excluded.yield_pct, total_amt=excluded.total_amt, "
        "  ex_div_dt=excluded.ex_div_dt, board_dt=excluded.board_dt, "
        "  raw_sha256=excluded.raw_sha256, needs_review=excluded.needs_review, "
        "  note=excluded.note "
        "WHERE excluded.rcept_no >= dividend.rcept_no",
        [(d.srtn_cd, d.record_dt, d.rcept_no, d.corp_code, d.itms_nm, d.report_nm,
          d.div_kind, d.div_type, d.dps, d.dps_pref, d.yield_pct, d.total_amt,
          d.ex_div_dt, d.board_dt, d.raw_sha256, d.needs_review, d.note)
         for d in items])
    return len(items)


def mark_month(conn: sqlite3.Connection, ym: str, status: str, rows: int,
               message: str = "") -> None:
    """달 단위 훑기 상태. ``ingest_day`` 를 ``source='dart_dividend'`` 로 나눠 쓴다.

    ``bas_dt`` 칸에 ``YYYYMM`` 을 넣는다 — 표를 새로 만들지 않고 재개 로직을 그대로
    물려받기 위해서다. 포털의 날짜와 섞이지 않는 것은 ``source`` 가 다르기 때문이다.
    """
    conn.execute(
        "INSERT INTO ingest_day (source,bas_dt,status,rows,attempts,updated_at,message) "
        "VALUES ('dart_dividend',?,?,?,1,?,?) "
        "ON CONFLICT(source,bas_dt) DO UPDATE SET "
        "  status=excluded.status, rows=excluded.rows, "
        "  attempts=ingest_day.attempts+1, updated_at=excluded.updated_at, "
        "  message=excluded.message",
        (ym, status, rows, raw_store.now_kst_iso(), message))


def done_months(conn: sqlite3.Connection) -> Dict[str, sqlite3.Row]:
    return {r["bas_dt"]: r for r in conn.execute(
        "SELECT * FROM ingest_day WHERE source='dart_dividend'")}


# ==================================================
# 6. 종목코드 매핑
# ==================================================
def corp_code_map() -> Dict[str, List[str]]:
    """종목코드(6자리) → ``[corp_code, 회사명]``.

    ``corpCode.xml`` 을 한 번 받아 만들어 둔 파일을 읽는다. 없으면 **막다른 길로 만들지
    않고** 무엇을 하면 되는지 알려 준다.
    """
    p = config.DART_CORP_CODE_PATH
    if not p.exists():
        raise DartError(
            f"DART 기업 고유번호 매핑이 없다: {p}\n"
            "  왜 필요한가: 공시 목록은 corp_code(8자리)로 오고, 우리 시세 표는\n"
            "               종목코드(6자리)로 되어 있다. 둘을 잇는 표가 이 파일이다.\n"
            "  할 일: `python -m collector.dividend corp-code` 를 한 번 돌린다."
        )
    return json.loads(p.read_text(encoding="utf-8"))


def build_corp_code_map(limiter: RateLimiter, *,
                        session: Optional[requests.Session] = None) -> Dict[str, List[str]]:
    """``corpCode.xml`` (zip)을 받아 매핑 파일을 만든다. 호출 1회."""
    r = _get(limiter, config.DART_CORP_CODE_URL, {}, session=session, timeout=180)
    if r.status_code != 200:
        raise DartError(f"corpCode 조회 HTTP {r.status_code}")
    if not r.content.startswith(b"PK"):
        text = r.content.decode("utf-8", "replace")
        m = re.search(r"<status>(\d+)</status>", text)
        _check_status(m.group(1) if m else "?", text[:200], "corpCode")
        raise DartError(f"corpCode 응답이 zip 이 아니다: {text[:200]!r}")

    xml = _unzip(r.content, "corpCode")
    text = xml.decode("utf-8", "replace")
    out: Dict[str, List[str]] = {}
    for m in re.finditer(r"<list>(.*?)</list>", text, re.S):
        blk = m.group(1)
        code = re.search(r"<corp_code>(.*?)</corp_code>", blk)
        name = re.search(r"<corp_name>(.*?)</corp_name>", blk)
        stock = re.search(r"<stock_code>(.*?)</stock_code>", blk)
        sc = (stock.group(1).strip() if stock else "")
        if len(sc) == 6 and code:
            out[sc] = [code.group(1).strip(), (name.group(1).strip() if name else "")]
    config.ensure_dirs()
    config.DART_CORP_CODE_PATH.write_text(
        json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
    return out
