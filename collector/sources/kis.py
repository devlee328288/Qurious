"""한국투자증권 Open API — 실시간 시세·잔고 전용.

⚠️ 이 파일로 **과거 시세를 백필하지 않는다.** 약관을 읽고 내린 결정이다(#33).

  · 제5조③ — 시세는 "개인의 업무에 한하여" 쓸 수 있고 **제3자 제공이 금지**된다.
    팀 저장소에 올려 공유하는 행위가 여기 걸릴 수 있다.
  · 제9조①4 — **서버에 부하를 주면 이용을 중지**시킬 수 있다. 수천 번 호출하는 백필이
    정확히 그 모양이다.
  · 제12조 — 유량은 "회사가 정하여 게시" 한다는데 **게시된 수치가 없다.**
    모르는 한도에 대량 호출을 거는 것은 이용 중지를 자초하는 일이다.

과거 시세는 공공데이터포털에서 받는다(sources/portal.py). KIS 는 포털이 못 주는 것만
맡는다 — **당일 실시간 시세**와 **모의투자 주문·잔고**다. 포털은 T+1 이후에나 자료를
주므로 둘은 겹치지 않고 서로를 메운다.

무엇을 재사용했나
-----------------
``app/services/brokers/kis.py``(190줄)의 **엔드포인트 경로·TR ID·응답 필드 이름**을
그대로 쓴다. 코드는 다시 썼다 — 그쪽은 ``httpx`` 비동기에 앱 설정(``app.config``)을
import 하므로, 스택 없이 도는 이 수집기에서는 쓸 수 없다. 자세한 판정은
collector/README.md 의 표에 적었다.

⚠️ 반드시 ``rt_cd`` 를 먼저 본다
--------------------------------
KIS 는 **실패해도 HTTP 200 으로 주는 경우가 있고**, 유량 초과(``EGW00201``)는 HTTP 500
으로 오면서 ``output2`` 가 **빈 배열**로 채워져 온다(#33 실측). 즉 상태 코드만 보거나
``output`` 만 꺼내 쓰면 **조용히 빈 결과를 참으로 받아들인다.** 그래서 이 파일의 모든
응답 처리는 ``rt_cd`` 검사부터 시작한다.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Dict, Optional

import requests

from collector import config
from collector.ratelimit import RateLimiter

#: 유량 초과 코드. 이걸 만나면 잠깐 쉬었다 다시 부른다.
RATE_LIMIT_CODES = {"EGW00201"}


class KISError(RuntimeError):
    """KIS 가 정상 응답을 주지 않았다."""


@dataclass
class Quote:
    symbol: str
    name: str
    close: int
    open: int
    high: int
    low: int
    volume: int
    change: int
    change_pct: float
    halted: bool


class KISClient:
    """모의투자 기본. 실계좌는 부르는 쪽이 명시적으로 켜야 한다."""

    def __init__(self, *, paper: bool = True, limiter: Optional[RateLimiter] = None):
        self.paper = paper
        self.base_url = config.KIS_MOCK_URL if paper else config.KIS_REAL_URL
        self.app_key, self.app_secret, self.account_no = config.kis_credentials(paper)
        self.limiter = limiter or RateLimiter(
            config.KIS_SLEEP, name="KIS모의" if paper else "KIS실계좌")
        self.session = requests.Session()
        self._token: Optional[str] = None
        self._token_exp: float = 0.0

    # ==================================================
    # 토큰 — 24시간짜리라 캐시한다
    # ==================================================
    def _load_cached_token(self) -> bool:
        """디스크에 남은 토큰을 쓴다.

        매번 새로 발급받으면 그 호출만으로 유량을 쓰고, KIS 는 짧은 시간에 반복 발급을
        거절하기도 한다. 만료까지 5분 이상 남은 것만 쓴다 — 쓰는 도중에 만료되면 그
        요청이 통째로 실패하기 때문이다.
        """
        p = config.KIS_TOKEN_CACHE
        if not p.exists():
            return False
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if d.get("paper") is not self.paper or d.get("app_key") != self.app_key[:8]:
            return False           # 다른 계정의 토큰이다
        if float(d.get("expires_at", 0)) - time.time() < 300:
            return False
        self._token, self._token_exp = d["access_token"], float(d["expires_at"])
        return True

    def _save_token(self) -> None:
        config.ensure_dirs()
        config.KIS_TOKEN_CACHE.write_text(json.dumps({
            "paper": self.paper,
            "app_key": self.app_key[:8],     # 어느 계정 것인지 알아볼 만큼만. 키는 안 담는다
            "access_token": self._token,
            "expires_at": self._token_exp,
        }), encoding="utf-8")

    def token(self) -> str:
        if self._token and self._token_exp - time.time() > 300:
            return self._token
        if self._load_cached_token():
            return self._token

        self.limiter.wait()
        r = self.session.post(f"{self.base_url}/oauth2/tokenP", timeout=20, json={
            "grant_type": "client_credentials",
            "appkey": self.app_key, "appsecret": self.app_secret,
        })
        if r.status_code != 200:
            raise KISError(
                f"토큰 발급 실패: HTTP {r.status_code} {r.text[:200]}\n"
                "  자주 겪는 원인: 같은 키로 짧은 시간에 여러 번 발급하면 거절된다.\n"
                f"  할 일: 잠시 뒤 다시 시도한다. 캐시 파일은 {config.KIS_TOKEN_CACHE} 이다."
            )
        d = r.json()
        self._token = d["access_token"]
        self._token_exp = time.time() + float(d.get("expires_in", 86400))
        self._save_token()
        return self._token

    # ==================================================
    # 호출 — rt_cd 를 먼저 본다
    # ==================================================
    def _headers(self, tr_id: str) -> Dict[str, str]:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token()}",
            "appkey": self.app_key, "appsecret": self.app_secret,
            "tr_id": tr_id, "custtype": "P",
        }

    def get(self, path: str, tr_id: str, params: Dict, *, retries: int = 3) -> Dict:
        """GET 한 번. 유량 초과면 기다렸다 다시 부른다.

        ``rt_cd`` 가 ``"0"`` 이 아니면 **예외를 던진다.** 빈 결과를 정상처럼 돌려주지
        않는 것이 이 함수의 존재 이유다.
        """
        last = ""
        for attempt in range(retries):
            self.limiter.wait()
            r = self.session.get(f"{self.base_url}{path}", headers=self._headers(tr_id),
                                 params=params, timeout=20)
            try:
                d = r.json()
            except ValueError:
                last = f"JSON 이 아니다: HTTP {r.status_code} {r.text[:160]}"
                continue

            rt, code, msg = str(d.get("rt_cd", "")), d.get("msg_cd", ""), d.get("msg1", "")
            if rt == "0":
                return d
            last = f"rt_cd={rt} msg_cd={code} msg1={msg!r} (HTTP {r.status_code})"
            if code in RATE_LIMIT_CODES:
                # 초당 약 2건이 한계였다(실측). 재시도마다 배로 늘려 기다린다.
                wait = config.KIS_SLEEP * (2 ** (attempt + 1))
                time.sleep(wait)
                continue
            break          # 유량 문제가 아니면 다시 불러도 같은 답이 온다

        raise KISError(
            f"KIS 호출 실패: {path}\n  {last}\n"
            "  ⚠️ HTTP 200 이어도 rt_cd 가 0 이 아니면 실패다. 유량 초과(EGW00201)는\n"
            "     HTTP 500 에 output2 가 빈 배열로 와서, 상태 코드만 보면 조용히 틀린다.\n"
            "  할 일: 호출 간격(config.KIS_SLEEP)을 늘리거나 잠시 뒤 다시 시도한다."
        )

    # ==================================================
    # 시세
    # ==================================================
    def quote(self, code: str) -> Quote:
        """현재가 한 종목.

        ⚠️ 거래정지 판별이 **포털과 다르다.** 포털은 시가와 거래량이 함께 0 으로 오지만,
           KIS 는 누적거래량 ``acml_vol`` 이 0 으로 나타난다(#33). 한쪽 규칙을 다른 쪽에
           그대로 쓰면 정지일을 통째로 놓친다.
        """
        d = self.get("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                     {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        o = d.get("output") or {}
        if not o:
            raise KISError(f"rt_cd 는 0 인데 output 이 비었다: {code} — 응답 모양이 바뀌었을 수 있다")

        def i(k: str) -> int:
            try:
                return int(float(str(o.get(k, "0")).replace(",", "") or 0))
            except ValueError:
                return 0

        vol = i("acml_vol")
        return Quote(
            symbol=code, name=o.get("hts_kor_isnm", ""),
            close=i("stck_prpr"), open=i("stck_oprc"), high=i("stck_hgpr"), low=i("stck_lwpr"),
            volume=vol, change=i("prdy_vrss"),
            change_pct=float(str(o.get("prdy_ctrt", "0")) or 0),
            halted=(vol == 0),
        )

    def balance(self) -> Dict:
        """계좌 잔고.

        ⚠️ 약관 제13조 — **점검 시간에는 잔고가 실제와 다를 수 있다.** 점검 중에 받은
           숫자로 주문을 내면 실제 보유와 어긋난다. 자동매매를 붙일 때 이 창을 피해야
           한다(D7 #13).
        """
        cano, prdt = self.account_no[:8], self.account_no[8:]
        tr = "VTTC8434R" if self.paper else "TTTC8434R"
        return self.get("/uapi/domestic-stock/v1/trading/inquire-balance", tr, {
            "CANO": cano, "ACNT_PRDT_CD": prdt, "AFHR_FLPR_YN": "N", "OFL_YN": "",
            "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        })


def main() -> int:
    """연결 확인. **모의 계정으로만** 돈다."""
    cli = KISClient(paper=True)
    print(f"모의 도메인 {cli.base_url}")
    for code, name in [("005930", "삼성전자"), ("000660", "SK하이닉스"), ("035720", "카카오")]:
        q = cli.quote(code)
        print(f"  {code} {q.name or name:<10} 종가 {q.close:>9,} · 거래량 {q.volume:>12,}"
              + ("  ⚠️ 거래 없음" if q.halted else ""))
    print(cli.limiter.report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
