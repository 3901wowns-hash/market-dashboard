"""시장 지표 수집 → market/market.json
지수·환율·원자재: yfinance / 美 금리: FRED(키 불필요 CSV) / 국내 금리·크레딧: 한국은행 ECOS(.env의 ECOS_API_KEY)
전일 종가와 전일 대비(지수류 %, 금리·스프레드 bp)를 계산한다. 노션 입력은 Claude가 market.json을 읽어서 한다.
"""
import csv, io, json, os, re, sys, urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "market.json")
DATA = os.path.join(HERE, "data.json")  # 대시보드 페이지가 읽는 파일 (최신값 + 약 3개월 추이)
SERIES = {}  # --days N 을 주면 N영업일치(백필용)

# (코드, 이름, 구분, yfinance 티커, 단위)
YF = [
    ("KOSPI", "KOSPI", "국내주식", "^KS11", "pt"),
    ("KOSDAQ", "KOSDAQ", "국내주식", "^KQ11", "pt"),
    ("SPX", "S&P500", "해외주식", "^GSPC", "pt"),
    ("NDX", "나스닥", "해외주식", "^IXIC", "pt"),
    ("N225", "닛케이225", "해외주식", "^N225", "pt"),
    ("SSEC", "상해종합", "해외주식", "000001.SS", "pt"),
    ("VIX", "VIX", "변동성", "^VIX", "pt"),
    ("USDKRW", "원/달러", "환율", "KRW=X", "원"),
    ("DXY", "달러인덱스", "환율", "DX-Y.NYB", "pt"),
    ("JPYKRW", "엔/원(100엔)", "환율", "JPYKRW=X", "원"),
    ("WTI", "WTI", "원자재", "CL=F", "$"),
    ("BRENT", "브렌트유", "원자재", "BZ=F", "$"),
    ("GOLD", "금", "원자재", "GC=F", "$"),
    ("COPPER", "구리", "원자재", "HG=F", "$"),
]
# FRED 시리즈 (%, 변화는 bp)
FRED = [("US2Y", "美 국채 2년", "DGS2"), ("US10Y", "美 국채 10년", "DGS10")]
# ECOS (통계코드, 항목코드, 주기)
ECOS = [
    ("KTB3Y", "국고 3년", "국내금리", "817Y002", "010200000", "D"),
    ("KTB10Y", "국고 10년", "국내금리", "817Y002", "010210000", "D"),
    ("CD91", "CD 91일", "국내금리", "817Y002", "010502000", "D"),
    ("CORP_AA", "회사채 AA- 3년", "크레딧", "817Y002", "010300000", "D"),
    ("CORP_BBB", "회사채 BBB- 3년", "크레딧", "817Y002", "010320000", "D"),
]


def env(key):
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            m = re.match(rf"\s*{key}\s*=\s*(.*)", line)
            if m:
                return m.group(1).strip().strip("\"'")
    return os.environ.get(key, "")


def row(code, name, cat, unit, last, prev, kind):
    """kind: 'pct' = 등락률 %, 'bp' = 금리 차 bp"""
    (d1, v1), (d0, v0) = last, prev
    chg = (v1 / v0 - 1) * 100 if kind == "pct" else (v1 - v0) * 100
    chg = round(chg, 2 if kind == "pct" else 1)
    return {"code": code, "name": name, "cat": cat, "unit": unit, "date": d1, "value": round(v1, 3),
            "prev_date": d0, "prev_value": round(v0, 3), "change": chg,
            "change_unit": "%" if kind == "pct" else "bp",
            "dir": "▲" if chg > 0 else "▼" if chg < 0 else "―"}


NDAYS = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 1


def emit(code, name, cat, unit, pts, kind):
    """최근 NDAYS 영업일치 행 생성 (기본 1 = 전일 종가만)"""
    SERIES[code] = pts
    return [row(code, name, cat, unit, pts[-k], pts[-k - 1], kind) for k in range(1, min(NDAYS, len(pts) - 1) + 1)]


def fetch_yf():
    import yfinance as yf
    out, errs = [], []
    for code, name, cat, tk, unit in YF:
        try:
            h = yf.Ticker(tk).history(period="130d", auto_adjust=False)["Close"].dropna()
            today = datetime.now().strftime("%Y-%m-%d")  # 오늘자 봉은 장중 값이라 제외 (전일 종가 기준)
            mul = 100 if code == "JPYKRW" else 1
            pts = [(i.strftime("%Y-%m-%d"), float(v) * mul) for i, v in h.items() if i.strftime("%Y-%m-%d") < today]
            if len(pts) < 2:
                raise ValueError("데이터 부족")
            out += emit(code, name, cat, unit, pts, "pct")
        except Exception as e:
            errs.append(f"{code}: {e}")
    return out, errs


def fetch_fred():
    out, errs = [], []
    start = (datetime.now() - timedelta(days=130)).strftime("%Y-%m-%d")
    for code, name, sid in FRED:
        try:
            u = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}"
            txt = urllib.request.urlopen(u, timeout=30).read().decode()
            pts = [(r[0], float(r[1])) for r in list(csv.reader(io.StringIO(txt)))[1:] if len(r) > 1 and r[1] not in ("", ".")]
            out += emit(code, name, "해외금리", "%", pts, "bp")
        except Exception as e:
            errs.append(f"{code}: {e}")
    return out, errs


def fetch_ecos():
    key = env("ECOS_API_KEY")
    if not key:
        return [], ["ECOS_API_KEY 없음"]
    out, errs = [], []
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=130)).strftime("%Y%m%d")
    for code, name, cat, stat, item, cyc in ECOS:
        try:
            u = f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/100/{stat}/{cyc}/{start}/{end}/{item}"
            rows = json.load(urllib.request.urlopen(u, timeout=30))["StatisticSearch"]["row"]
            pts = [(f"{r['TIME'][:4]}-{r['TIME'][4:6]}-{r['TIME'][6:8]}", float(r["DATA_VALUE"])) for r in rows]
            out += emit(code, name, cat, "%", pts, "bp")
        except Exception as e:
            errs.append(f"{code}: {e}")
    return out, errs


def derived(items):
    """장단기·크레딧 스프레드 (bp). 같은 기준일의 두 지표가 모두 있는 날만 계산."""
    by = {}
    for i in items:
        by.setdefault(i["code"], {})[i["date"]] = i
    specs = [("US_2S10S", "美 장단기(10y−2y)", "해외금리", "US10Y", "US2Y"),
             ("KR_3S10S", "韓 장단기(10y−3y)", "국내금리", "KTB10Y", "KTB3Y"),
             ("SPR_AA", "신용스프레드 AA-(−국고3년)", "크레딧", "CORP_AA", "KTB3Y"),
             ("SPR_BBB", "신용스프레드 BBB-(−국고3년)", "크레딧", "CORP_BBB", "KTB3Y")]
    out = []
    for code, name, cat, a, b in specs:
        for d, x in sorted(by.get(a, {}).items()):
            y = by.get(b, {}).get(d)
            if not y or x["prev_date"] != y["prev_date"]:
                continue
            v1, v0 = (x["value"] - y["value"]) * 100, (x["prev_value"] - y["prev_value"]) * 100
            out.append({"code": code, "name": name, "cat": cat, "unit": "bp", "date": d, "value": round(v1, 1),
                        "prev_date": x["prev_date"], "prev_value": round(v0, 1), "change": round(v1 - v0, 1),
                        "change_unit": "bp", "dir": "▲" if v1 > v0 else "▼" if v1 < v0 else "―"})
    return out


def main():
    items, errors = [], []
    for fn in (fetch_yf, fetch_fred, fetch_ecos):
        r, e = fn()
        items += r
        errors += e
    items += derived(items)
    # 파생 지표(스프레드) 추이: 두 시계열의 공통 날짜
    for code, a, b in [("US_2S10S", "US10Y", "US2Y"), ("KR_3S10S", "KTB10Y", "KTB3Y"),
                       ("SPR_AA", "CORP_AA", "KTB3Y"), ("SPR_BBB", "CORP_BBB", "KTB3Y")]:
        if a in SERIES and b in SERIES:
            yb = dict(SERIES[b])
            SERIES[code] = [(d, round((v - yb[d]) * 100, 1)) for d, v in SERIES[a] if d in yb]
    page = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "items": [i for i in items if i["date"] == max(j["date"] for j in items if j["code"] == i["code"])],
            "series": {c: [[d, round(v, 3)] for d, v in pts[-90:]] for c, pts in SERIES.items()}}
    json.dump(page, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    # 데이터가 지난 게시와 같으면(휴장일 등) 게시를 건너뛰기 위한 표시
    import hashlib
    h = hashlib.sha1(json.dumps([page["items"], page["series"]], sort_keys=True).encode()).hexdigest()
    pub, pend = os.path.join(HERE, ".published_hash"), os.path.join(HERE, ".pending_hash")
    same = os.path.exists(pub) and open(pub).read().strip() == h
    if same and os.path.exists(pend):
        os.remove(pend)
    elif not same:
        open(pend, "w").write(h)
    data = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "items": items, "errors": errors}
    json.dump(data, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[fetch_market] {len(items)}개 저장, 실패 {len(errors)}건 → {OUT}")
    for e in errors:
        print("  !!", e)
    return 0 if items else 1


if __name__ == "__main__":
    sys.exit(main())
