"""호출 간격과 하루 한도.

두 가지를 한다
--------------
1. **간격** — 호출 사이에 최소 시간을 둔다. 상대 서버를 배려하는 일이자, 우리가
   막히지 않는 길이다.
2. **한도** — 하루에 쓸 수 있는 횟수를 넘기지 않는다. 넘기면 그날 남은 작업이 통째로
   실패하므로, 넘기기 **전에** 멈춘다.

포털 유량에 대해 실제로 알아낸 것
---------------------------------
응답 헤더가 남은 횟수를 실어 준다 — ``X-RateLimit-Limit: 10000``,
``X-RateLimit-Remaining``. 문서에 적힌 값이 아니라 **응답이 알려 주는 값**이므로 이쪽을
믿는다.

리셋 주기는 관측으로 좁혔다(모두 KST):

    09-18 14:4x  9,790
    09-18 17:16  9,789
    09-18 18:14  9,788
    09-18 19:26  9,770
    09-18 종료   9,757     ← 4시간 40분 동안 **한 번도 회복되지 않음**
    09-19 11:03  9,999     ← 한 번 쓰고 남은 값. 즉 그 직전엔 10,000

첫 다섯 줄이 짧은 주기 리셋(시간당·분당)을 배제한다. 마지막 줄이 **24시간 롤링을
배제한다** — 롤링이라면 09-18 14:4x 의 호출은 09-19 11:03 시점에 아직 24시간이 안
지났으므로 9,757 근처여야 한다.

⚠️ 남는 것은 **고정 창**이다. 다만 창이 갈리는 시각이 자정인지 새벽 몇 시인지는 이
   관측만으로 특정할 수 없다 — 09-18 19:26 과 09-19 11:03 사이 어딘가다. 수집기는
   그 시각을 **알 필요가 없게** 짰다: 헤더가 주는 남은 횟수만 보고 판단한다.
"""

from __future__ import annotations

import time
from typing import Optional


class BudgetExhausted(RuntimeError):
    """하루 한도를 (거의) 다 썼다. 오늘은 더 부르지 않는다."""


class RateLimiter:
    """한 출처에 대한 간격·한도 관리자.

    ``remaining`` 은 응답 헤더를 받을 때마다 갱신된다. 헤더가 없는 출처(KIS 등)는
    끝까지 ``None`` 으로 남고, 그때는 간격만 지킨다.
    """

    def __init__(self, min_interval: float, *, reserve: int = 0, name: str = ""):
        self.min_interval = min_interval
        self.reserve = reserve
        self.name = name or "source"
        self.remaining: Optional[int] = None
        self.limit: Optional[int] = None
        self.calls = 0
        self._last = 0.0

    def wait(self) -> None:
        """직전 호출로부터 최소 간격이 지날 때까지 기다린다."""
        gap = time.monotonic() - self._last
        if self._last and gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()
        self.calls += 1

    def observe(self, headers) -> None:
        """응답 헤더에서 남은 횟수를 읽는다. 없으면 조용히 넘어간다."""
        for k, v in headers.items():
            kl = k.lower()
            try:
                if kl == "x-ratelimit-remaining":
                    self.remaining = int(v)
                elif kl == "x-ratelimit-limit":
                    self.limit = int(v)
            except (TypeError, ValueError):
                # 값이 숫자가 아니면 모르는 것으로 둔다. 추측해서 멈추는 쪽이 더 나쁘다.
                pass

    def check_budget(self, need: int = 1) -> None:
        """남은 횟수가 예비분 아래로 내려가면 **부르기 전에** 멈춘다.

        예비분을 두는 이유는, 수집이 한도를 바닥까지 쓰면 그날 사람이 손으로 확인 한 번
        해 볼 여유도 없어지기 때문이다.
        """
        if self.remaining is None:
            return
        if self.remaining - need < self.reserve:
            raise BudgetExhausted(
                f"[{self.name}] 하루 한도가 거의 소진됐다 — 남은 {self.remaining:,}, "
                f"예비로 남길 몫 {self.reserve:,}\n"
                "  왜 멈추나: 바닥까지 쓰면 오늘 확인 작업까지 막힌다.\n"
                "  할 일: 내일 다시 돌린다. 진행 상태는 ingest_day 표에 남아 있어\n"
                "         멈춘 자리에서 이어서 간다. 급하면 config.PORTAL_RESERVE 를 낮춘다."
            )

    def report(self) -> str:
        if self.remaining is None:
            return f"[{self.name}] 호출 {self.calls:,}회 (남은 횟수 정보 없음)"
        return (f"[{self.name}] 호출 {self.calls:,}회 · 남은 유량 {self.remaining:,}"
                + (f"/{self.limit:,}" if self.limit else ""))
