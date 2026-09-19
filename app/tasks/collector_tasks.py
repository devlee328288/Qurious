"""수집기를 Celery Beat 에 얹는 어댑터 — **얇게** 둔다.

이 파일에는 로직이 없다. 전부 ``collector`` 패키지가 하고, 여기서는 일정만 붙인다.
그래야 수집기가 스택 없이도 돌아간다(collector/config.py 머리말).

세 가지 방법 중 무엇을 쓸 것인가
--------------------------------
========================  ==================================  ======================
방법                      언제 맞나                            대가
========================  ==================================  ======================
``python -m collector…``  스택을 안 띄우고 지금 당장 받을 때    사람이 직접 실행
Windows 작업 스케줄러      혼자 쓰는 노트북에서 매일 돌릴 때      OS 에 묶인다
**Celery Beat (이 파일)**  앱과 함께 배포할 때                  Redis·워커가 떠 있어야
========================  ==================================  ======================

⚠️ 원래 구상은 APScheduler 였다(th03 ``scheduler.py`` 선례). 그런데 이 저장소에는
   **이미 Celery + Redis 가 있다** — ``app/celery_app.py`` 에 Beat 스케줄이 두 개
   돌고 있고 ``timezone="Asia/Seoul"`` 까지 잡혀 있다. 스케줄러를 하나 더 들이면
   같은 일을 하는 물건이 둘이 되고, 어느 쪽이 안 돌았는지 찾는 일이 늘어난다.
   APScheduler 를 굳이 쓰고 싶다면 ``collector.backfill.run_recent()`` 를 부르기만
   하면 된다 — 코어는 스케줄러를 모른다.

왜 "당일 14:00" 이 아닌가
-------------------------
포털은 당일 시세를 당일에 주지 않는다. 2026-09-19(토) 11:00 KST 에 09-18(금) 시세가
**아직 없었다**(실측). 당일 ``basDt`` 를 찍는 일정은 영원히 0건을 받는다.

그래서 ``recent`` 모드로 **최근 2주를 되돌아보며 빈 날만 채운다.** 지연이 하루든
이틀이든 알아서 메워지고, 연휴 뒤에도 빠진 날이 그 창 안에 들어온다. 지연의 정확한
길이를 알아낼 필요가 없어지는 것이 요점이다.
"""
import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, name="collector.portal_recent", max_retries=0, time_limit=1800)
def portal_recent_task(self, window: int = 14) -> dict:
    """최근 창 안에서 빠진 기준일을 채운다. 하루 한 번이면 충분하다.

    ``max_retries=0`` 인 이유: 실패한 날은 ``ingest_day`` 에 ``error`` 로 남고 **다음
    실행이 그 날만 다시 받는다.** Celery 재시도까지 걸면 같은 일을 두 겹으로 하면서
    유량만 더 쓴다.
    """
    from collector.backfill import run_recent
    t = run_recent(window=window, quiet=True)
    logger.info("포털 수집: 적재 %s일 · 빈응답 %s일 · 실패 %s일 · %s행",
                t.get("done"), t.get("empty"), t.get("error"), t.get("rows"))
    return t


@celery_app.task(bind=True, name="collector.rebuild_adjusted", max_retries=0, time_limit=3600)
def rebuild_adjusted_task(self) -> dict:
    """수정주가를 다시 만든다. **수집 뒤에 돌아야 한다.**

    조정은 과거 전체를 다시 쓰는 일이라 새 거래일이 하나 들어올 때마다 필요하다.
    분할·권리락이 있던 날이 새로 들어오면 그 이전 전체의 계수가 바뀐다.
    """
    from collector import db, preprocess
    conn = db.connect()
    try:
        t = preprocess.rebuild(conn)
    finally:
        conn.close()
    logger.info("수정주가: 종목 %s · 행 %s · 이벤트 %s (확인필요 %s)",
                t.get("codes"), t.get("rows"), t.get("events"), t.get("review"))
    return t


@celery_app.task(bind=True, name="collector.write_manifest", max_retries=0, time_limit=600)
def write_manifest_task(self) -> dict:
    """수집물 지문을 파일로 남긴다. 팀원과 "같은 자료를 봤는가" 를 맞출 때 쓴다."""
    from collector import db, manifest
    conn = db.connect()
    try:
        m = manifest.build(conn)
        path = manifest.write(conn)
    finally:
        conn.close()
    logger.info("매니페스트 %s · 기준일 %s일 · 지문 %s", path, m["day_count"], m["rollup_sha256"][:16])
    return {"path": path, "day_count": m["day_count"], "rollup_sha256": m["rollup_sha256"]}
