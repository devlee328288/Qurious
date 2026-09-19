"""매니페스트 — 누가 무엇을 언제 받았는지, 그리고 그것이 지금도 같은지.

왜 필요한가
-----------
팀원 네 사람이 각자 수집기를 돌리면 **같은 날 같은 API 를 불러도 결과가 다를 수 있다.**
출처가 과거 값을 정정하기도 하고, 누군가는 중간에 끊긴 채로 두기도 한다. 그런데 분석
결과만 주고받으면 그 차이가 드러나지 않는다 — 수치가 다른 이유를 모델 탓으로 돌리다가
몇 시간을 버린다.

매니페스트는 각자의 수집물에 **지문**을 찍는다. 두 사람의 지문이 같으면 같은 자료를 본
것이고, 다르면 어느 날이 다른지가 바로 나온다.

무엇을 담나
-----------
날짜별로 ``원문 sha256`` · ``행 수`` · ``받은 시각`` 만 담는다. **값 자체는 담지 않는다.**
그래서 매니페스트 파일은 저장소에 올려도 된다 — 원자료가 아니라 원자료의 지문이다.

RAW_SHARING — 원자료를 밖으로 내보낼 것인가
-------------------------------------------
🔴 **기본은 끔이다.** 그리고 켜기 전에 답해야 할 질문이 남아 있다.

  · 공공데이터포털 자료는 공공누리 조건이 붙는다. "변경금지" 조항이 지표 계산까지
    막는지가 아직 불분명하다(#33 열린 질문).
  · KIS 시세는 약관 제5조③ 이 **제3자 제공을 금지**한다. 그런데 "팀원이 제3자인가" 가
    판단되지 않았다 — 1차 프로젝트 주석은 "private 저장소면 제3자 제공이 아니다" 로
    적혀 있었고, 2026-09 에 약관을 다시 읽은 결과는 "팀원도 제3자일 수 있다" 였다.
    **법률 판단이라 우리가 정할 일이 아니다. 강사님께 여쭐 항목이다.**

그래서 이 스위치는 **켜는 방법만 있고 기본값은 꺼짐**이다. 판단이 서기 전까지 팀은
매니페스트(지문)만 주고받으면 된다 — 그것만으로 "같은 자료를 봤는가" 는 확인된다.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Dict, List, Optional

from collector import config, db, raw_store

#: 원자료(응답 원문)를 저장소·허브로 내보내도 되는가.
#: 환경변수로만 켠다 — 코드에 상수로 박아 두면 실수로 켜진 채 커밋된다.
RAW_SHARING = os.environ.get("QURIOUS_RAW_SHARING", "").lower() in ("1", "true", "yes")

MANIFEST_VERSION = 1


def build(conn: sqlite3.Connection, source: str = "portal") -> Dict:
    """지금 DB 상태의 매니페스트를 만든다."""
    days: List[Dict] = []
    for r in conn.execute(
            "SELECT i.bas_dt, i.status, i.rows, i.updated_at, "
            "       (SELECT sha256 FROM raw_response w "
            "        WHERE w.source=i.source AND w.target='price/'||i.bas_dt "
            "        ORDER BY w.fetched_at DESC LIMIT 1) AS sha, "
            "       (SELECT fetched_at FROM raw_response w "
            "        WHERE w.source=i.source AND w.target='price/'||i.bas_dt "
            "        ORDER BY w.fetched_at DESC LIMIT 1) AS fetched_at "
            "FROM ingest_day i WHERE i.source=? ORDER BY i.bas_dt", (source,)):
        days.append({"bas_dt": r["bas_dt"], "status": r["status"], "rows": r["rows"],
                     "sha256": r["sha"], "fetched_at": r["fetched_at"]})

    # 전체 지문 — 날짜별 지문을 순서대로 이어 붙여 한 번 더 해시한다.
    # 이 값 하나만 비교하면 "전부 같은가" 를 한눈에 본다.
    roll = hashlib.sha256()
    for d in days:
        roll.update(f"{d['bas_dt']}:{d['sha256'] or ''}:{d['rows']}\n".encode("utf-8"))

    return {
        "manifest_version": MANIFEST_VERSION,
        "source": source,
        "generated_at": raw_store.now_kst_iso(),
        "raw_sharing": RAW_SHARING,
        "day_count": len(days),
        "row_total": sum(d["rows"] for d in days),
        "rollup_sha256": roll.hexdigest(),
        "days": days,
    }


def write(conn: sqlite3.Connection, source: str = "portal") -> str:
    """매니페스트를 파일로 남기고 경로를 돌려준다."""
    m = build(conn, source)
    config.ensure_dirs()
    path = config.MANIFEST_DIR / f"{source}.json"
    path.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def compare(mine: Dict, theirs: Dict) -> Dict[str, List[str]]:
    """두 매니페스트를 맞춰 본다. **어느 날이 다른지**를 돌려준다.

    "다르다" 를 세 갈래로 나눈다 — 나만 있는 날, 상대만 있는 날, 둘 다 있는데 지문이
    다른 날. 셋의 원인이 서로 다르기 때문이다: 앞의 둘은 수집 범위 차이이고, 마지막
    하나는 **같은 날짜의 자료가 실제로 달라진 것**이라 훨씬 중요하다.
    """
    a = {d["bas_dt"]: d for d in mine.get("days", [])}
    b = {d["bas_dt"]: d for d in theirs.get("days", [])}
    only_mine = sorted(set(a) - set(b))
    only_theirs = sorted(set(b) - set(a))
    differ = sorted(d for d in set(a) & set(b)
                    if a[d].get("sha256") != b[d].get("sha256"))
    return {"only_mine": only_mine, "only_theirs": only_theirs, "sha_differ": differ}


def verify(conn: sqlite3.Connection, source: str = "portal") -> List[str]:
    """보존된 원문을 실제로 풀어 지문을 다시 계산한다.

    매니페스트가 맞다고 말하는 것과 디스크의 바이트가 실제로 맞는 것은 다르다.
    디스크는 말없이 비트를 바꾼다.
    """
    return raw_store.verify_all(conn, source)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m collector.manifest",
                                description="수집물 지문 생성·대조·검증")
    p.add_argument("action", choices=["write", "compare", "verify"])
    p.add_argument("--source", default="portal")
    p.add_argument("--theirs", help="compare: 상대방 매니페스트 파일 경로")
    a = p.parse_args(argv)

    conn = db.connect()
    if a.action == "write":
        m = build(conn, a.source)
        path = write(conn, a.source)
        print(f"매니페스트 저장: {path}")
        print(f"  기준일 {m['day_count']:,}일 · {m['row_total']:,}행")
        print(f"  전체 지문 {m['rollup_sha256']}")
        print(f"  원자료 공유(RAW_SHARING): {'켜짐 ⚠️' if m['raw_sharing'] else '꺼짐 (기본)'}")
    elif a.action == "compare":
        if not a.theirs:
            print("--theirs 로 상대 매니페스트 경로를 준다."); conn.close(); return 2
        mine = build(conn, a.source)
        theirs = json.loads(open(a.theirs, encoding="utf-8").read())
        d = compare(mine, theirs)
        if not any(d.values()):
            print("완전히 같다 — 같은 자료를 보고 있다.")
        for k, label in [("sha_differ", "⚠️ 같은 날인데 지문이 다름"),
                         ("only_mine", "나만 있는 날"), ("only_theirs", "상대만 있는 날")]:
            if d[k]:
                print(f"{label}: {len(d[k])}일  {', '.join(d[k][:10])}"
                      + (" …" if len(d[k]) > 10 else ""))
    else:
        bad = verify(conn, a.source)
        print(f"원문 무결성 검사 — 손상 {len(bad)}건" + (f": {bad[:5]}" if bad else " (이상 없음)"))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
