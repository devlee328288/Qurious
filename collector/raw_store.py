"""응답 원문 보존 — 다시 받지 않고 다시 정규화할 수 있게.

Alpha_Stack `common/raw_store.py`(300줄)에서 가져와 이 저장소에 맞게 줄였다.
무엇을 왜 바꿨는지는 collector/README.md 의 "재사용 판정" 표에 적어 두었다.

무엇을 막는가
-------------
정규화는 틀린다. 필드 이름을 잘못 매핑하고, 숫자 파싱이 어떤 값에서만 깨지고, 그때는
안 담은 칸이 나중에 필요해진다.

원문이 없으면 고치는 방법이 **다시 받는 것뿐**이다. 1,648 거래일을 다시 받는 것은 하루
한도의 상당 부분을 통째로 쓰는 일이고, 출처가 그사이 과거 값을 정정했다면 **같은 자료를
다시 받을 수도 없다.** 원문을 남겨 두면 네트워크를 한 번도 안 타고 정규화만 다시 돌린다.

두 번째 쓸모 — 언제부터 알 수 있었나
------------------------------------
``fetched_at`` 은 *"우리가 이 사실을 언제부터 알 수 있었나"* 의 근거다. 그 시각을
정규화 표에만 적어 두면 나중에 고쳐 적었는지 증명할 방법이 없다. 미래를 훔쳐본 모델은
성능이 좋아 보이기 때문에 **증명할 수 없는 시각은 없는 것과 같다.**

무엇을 저장하지 않나
--------------------
**기관이 API 로 내려주는 공공·금융 데이터만** 저장한다. 크롤링한 문서 본문은 넣지
않는다 — 기사 전문 보관은 저작권 문제다. 화이트리스트에 없는 출처는 조용히 넘어가지
않고 **예외로 막는다.**

⚠️ 응답을 **바이트 그대로** 담는다. 문자열로 바꿔 담으면 그 순간 인코딩 추측이 끼어들고
   (euc-kr 로 오는 곳이 실재한다), 잘못 디코딩한 원문은 더 이상 원문이 아니다.
"""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, List, Optional

KST = timezone(timedelta(hours=9))

#: 원문을 남겨도 되는 출처. 전부 기관이 API 로 내려주는 자료다.
#:
#: ⚠️ 여기에 크롤링 출처를 넣지 않는다. 넣고 싶어지면 그것은 규칙을 바꾸자는 뜻이므로
#:    팀이 먼저 정하고 나서 근거와 함께 추가한다.
#: ⚠️ KIS 는 약관 제5조③ 이 시세를 "개인의 업무에 한하여" 로 묶고 제3자 제공을 금지한다
#:    (#33). 그래서 KIS 원문은 **보존하되 밖으로 내보내지 않는다** — 공유 대상 판단은
#:    manifest 의 RAW_SHARING 스위치가 맡는다.
ALLOWED_SOURCES = frozenset({
    "portal",   # 공공데이터포털 금융위 주식시세
    "kis",      # 한국투자증권 Open API (실시간·주문)
    "ecos",     # 한국은행 경제통계
    "dart",     # 전자공시
    "krx",      # KRX Open API
})

#: 한 응답이 이보다 크면 저장하지 않고 예외를 낸다.
#:
#: 실측 2026-09-19: 포털 하루치 전종목 2,870행이 784,012 B → gzip 176,719 B (22.5%).
#: 즉 하루 약 172 KB, 1,648 거래일이면 약 278 MB 다. 상한 32 MB 는 정상 응답의 40배가
#: 넘으므로 평소에 걸리지 않고, **걸린다면 그건 요청이 잘못된 것**이다.
MAX_BODY_BYTES = 32 * 1024 * 1024


class SourceNotAllowed(ValueError):
    """원문을 남겨도 되는 출처가 아니다."""


class RawTooLarge(ValueError):
    """응답이 보존 상한을 넘었다."""


def now_kst_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _check_source(source: str) -> None:
    if source not in ALLOWED_SOURCES:
        raise SourceNotAllowed(
            f"원문을 보존할 수 없는 출처다: {source!r}\n"
            f"  보존 가능: {', '.join(sorted(ALLOWED_SOURCES))}\n"
            "  왜 막나: 크롤링으로 받은 문서 본문 보관은 저작권 문제다.\n"
            "  할 일: 공공·금융 API 라면 collector/raw_store.py 의 ALLOWED_SOURCES 에\n"
            "         근거와 함께 추가한다. 크롤링이라면 정규화 결과만 저장한다."
        )


def save(conn: sqlite3.Connection, source: str, target: str, body: bytes, *,
         http_status: Optional[int] = None, note: str = "",
         fetched_at: Optional[str] = None) -> str:
    """원문을 압축해 남기고 **압축 전 원문의 sha256** 을 돌려준다.

    ``conn`` 을 반드시 받는다 — Alpha_Stack 판은 연결을 스스로 열 수도 있었는데, 여기서는
    적재·원문·상태가 **한 트랜잭션에** 들어가야 재개가 거짓말을 하지 않으므로 그 길을
    아예 막았다.
    """
    _check_source(source)
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError(
            "원문은 bytes 여야 한다 — 문자열로 바꿔 담으면 인코딩 추측이 끼어든다.\n"
            "  할 일: requests 라면 `response.text` 가 아니라 `response.content` 를 넘긴다."
        )
    if len(body) > MAX_BODY_BYTES:
        raise RawTooLarge(
            f"응답이 보존 상한을 넘었다: {len(body):,} > {MAX_BODY_BYTES:,} 바이트\n"
            "  할 일: 요청 범위를 좁히거나, 정말 필요하면 MAX_BODY_BYTES 를 근거와 함께 올린다."
        )

    digest = hashlib.sha256(bytes(body)).hexdigest()
    # mtime 을 0 으로 고정한다 — 안 그러면 같은 원문을 같은 초에 두 번 압축해도 바이트가
    # 달라져 "바뀌었나?" 를 볼 때 헷갈린다.
    packed = gzip.compress(bytes(body), compresslevel=6, mtime=0)
    conn.execute(
        "INSERT OR REPLACE INTO raw_response "
        "(source, target, fetched_at, body, sha256, bytes, compression, http_status, note) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (source, target, fetched_at or now_kst_iso(), packed,
         digest, len(body), "gzip", http_status, note),
    )
    return digest


def _unpack(row: sqlite3.Row) -> Dict:
    """한 줄을 원문까지 풀어서 돌려준다. 지문이 안 맞으면 **조용히 넘어가지 않는다.**"""
    body = gzip.decompress(row["body"]) if row["compression"] == "gzip" else row["body"]
    actual = hashlib.sha256(body).hexdigest()
    if actual != row["sha256"]:
        raise ValueError(
            f"보존된 원문이 손상됐다: {row['source']} {row['target']} {row['fetched_at']}\n"
            f"  기록된 지문 {row['sha256'][:16]}… · 실제 {actual[:16]}…\n"
            "  할 일: 이 줄을 지우고 그 대상을 다시 받는다."
        )
    return {
        "source": row["source"], "target": row["target"],
        "fetched_at": row["fetched_at"], "body": body,
        "sha256": row["sha256"], "bytes": row["bytes"], "note": row["note"],
    }


def load(conn: sqlite3.Connection, source: str, target: str, *,
         fetched_at: Optional[str] = None) -> Optional[Dict]:
    """보존된 원문 하나. ``fetched_at`` 을 안 주면 **가장 최근에 받은 것**."""
    if fetched_at:
        row = conn.execute(
            "SELECT * FROM raw_response WHERE source=? AND target=? AND fetched_at=?",
            (source, target, fetched_at)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM raw_response WHERE source=? AND target=? "
            "ORDER BY fetched_at DESC LIMIT 1", (source, target)).fetchone()
    return _unpack(row) if row else None


def iter_latest(conn: sqlite3.Connection, source: str, *, prefix: str = "") -> Iterator[Dict]:
    """대상별 **가장 최근 원문**을 하나씩. 재정규화가 이 경로를 탄다.

    전부 메모리에 올리지 않는다 — 1,648건이면 그것만으로 1 GB 가 넘는다.
    """
    sql = ("SELECT * FROM raw_response WHERE source=? "
           "AND (?='' OR target LIKE ?||'%') "
           "AND fetched_at = (SELECT MAX(fetched_at) FROM raw_response r2 "
           "                  WHERE r2.source=raw_response.source AND r2.target=raw_response.target) "
           "ORDER BY target")
    for row in conn.execute(sql, (source, prefix, prefix)):
        yield _unpack(row)


def stats(conn: sqlite3.Connection) -> Dict[str, Dict]:
    """출처별 보존 현황과 **실제 차지하는 용량**. 추측값을 문서에 적지 않으려고 함께 낸다."""
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n, COUNT(DISTINCT target) AS targets, "
        "COALESCE(SUM(bytes),0) AS raw_bytes, COALESCE(SUM(LENGTH(body)),0) AS stored_bytes, "
        "MIN(fetched_at) AS first_at, MAX(fetched_at) AS last_at "
        "FROM raw_response GROUP BY source").fetchall()
    out: Dict[str, Dict] = {}
    for r in rows:
        rb, sb = r["raw_bytes"], r["stored_bytes"]
        out[r["source"]] = {
            "responses": r["n"], "targets": r["targets"],
            "raw_bytes": rb, "stored_bytes": sb,
            "ratio": round(sb / rb, 4) if rb else None,
            "first_at": r["first_at"], "last_at": r["last_at"],
        }
    return out


def verify_all(conn: sqlite3.Connection, source: str = "") -> List[str]:
    """보존된 원문을 전부 풀어 지문을 대조한다. 어긋난 대상 목록을 돌려준다.

    조용히 썩는 것을 막는 장치다 — 디스크는 말없이 비트를 바꾼다.
    """
    bad: List[str] = []
    sql = "SELECT * FROM raw_response" + (" WHERE source=?" if source else "")
    for row in conn.execute(sql, (source,) if source else ()):
        try:
            _unpack(row)
        except ValueError:
            bad.append(f"{row['source']}/{row['target']}@{row['fetched_at']}")
    return bad
