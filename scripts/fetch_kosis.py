"""KOSIS(국가통계포털) Open API에서 지역별 주택공급 지표를 받아 data/housing.json 으로 저장한다.

탭        KOSIS 통계표                                   '제외' 규모
인허가    DT_MLTM_1952 주택규모별 인허가실적(월별 누계)    40㎡이하   ← 누계를 월 값으로 환산
착공      DT_MLTM_5388 주택규모별 착공실적(월계)          40㎡이하
준공      DT_MLTM_5374 주택규모별 준공실적(월계)          40㎡이하
분양      DT_MLTM_5557 분양실적(공동주택, 순계·합계)      (규모 구분 없음)
미분양    DT_MLTM_2080 규모별 미분양현황                  40㎡이하 (공표 시작 이후만)
준공후    DT_MLTM_5328 공사완료후 미분양현황              40㎡이하 (공표 시작 이후만)

수도권·지방이 표에 없으면 시도 합산/전국-수도권으로 만든다.
광주·전남이 '전남광주'로 통합 발표되는 달이 있으므로, 전남광주가 없는 과거 달은 광주+전남으로 채운다.

사용법:
    $env:KOSIS_API_KEY="발급키"  또는 config.json {"kosis_api_key": "..."}
    python scripts/fetch_kosis.py
"""
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "housing.json")
LOG = os.path.join(ROOT, "data", "_fetch_log.json")
URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"

REGIONS = ["전국", "수도권", "지방", "서울", "인천", "경기", "부산", "대구", "광주", "대전", "울산", "세종",
           "강원", "충북", "충남", "전북", "전남", "전남광주", "경북", "경남", "제주"]
NAME = {"총계": "전국", "합계": "전국", "수도권소계": "수도권", "지방소계": "지방", "지방권": "지방"}


def api_key():
    k = os.environ.get("KOSIS_API_KEY", "").strip()
    cfg = os.path.join(ROOT, "config.json")
    if not k and os.path.exists(cfg):
        k = json.load(open(cfg, encoding="utf-8")).get("kosis_api_key", "").strip()
    if not k:
        sys.exit("[오류] KOSIS_API_KEY 환경변수(또는 config.json의 kosis_api_key)가 없습니다.")
    return k


def call(params, tries=6):
    q = urllib.parse.urlencode(params, safe="+")
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(URL + "?" + q, timeout=120) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                if str(data.get("err")) == "30":   # 데이터 없음
                    return []
                raise RuntimeError(f"KOSIS 오류 {data.get('err')}: {data.get('errMsg')}")
            return data
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"요청 실패({params.get('tblId')} {params.get('startPrdDe')}): {last}")


def fetch(key, tbl, objs, start, end, itm="ALL"):
    rows = []
    p = {"method": "getList", "apiKey": key, "orgId": "116", "tblId": tbl, "format": "json", "jsonVD": "Y",
         "prdSe": "M", "itmId": itm}
    for i, v in enumerate(objs, 1):
        p[f"objL{i}"] = v
    for y in range(int(start[:4]), int(end[:4]) + 1, 3):
        s = max(f"{y}01", start)
        e = min(f"{y + 2}12", end)
        rows += call(dict(p, startPrdDe=s, endPrdDe=e))
    return rows


def num(v):
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def reg(name):
    n = (name or "").replace(" ", "")
    n = NAME.get(n, n)
    return n if n in REGIONS else None


# ---- 표별 해석: row -> (region, field) / field: total | small | None
def x_permit(r):
    code = r["C4"].rsplit(".", 1)[1]
    # 0002 소계·0006 총합은 권역 조합마다 값이 달라 쓰지 않는다 → 수도권·지방은 시도 합산
    region = "전국" if code == "0001" else reg(r["C4_NM"])
    size = r["C1_NM"]
    return region, ("total" if size == "계" else "small" if size == "40㎡이하" else None)


def x_build(prefix):
    def f(r):
        code = r["C1"].rsplit(".", 1)[1]
        region = {"0001": "전국", "0002": "수도권", "0006": "지방"}.get(code)
        if not region and code not in ("0007", "0014"):  # 기타광역시·기타지방 제외
            region = reg(r["C1_NM"])
        size = r["C2_NM"]
        return region, ("total" if size == "계" else "small" if size == "40㎡이하" else None)
    return f


def x_sale(r):
    b = r["C2_NM"].replace(" ", "")
    if b == "합계":
        region = "전국" if r["C1_NM"] == "합계" else None   # 수도권·지방은 시도 합산
    elif b == "소계":
        region = None
    else:
        region = reg(b)
    return region, ("total" if r["C3_NM"] == "순계" else None)


def x_unsold(r):
    region = reg(r["C1_NM"])
    if r["C2_NM"] == "총합" and r["C3_NM"] == "총합":
        return region, "total"
    if r["C3_NM"] == "40㎡이하":
        return region, "small"
    return region, None


def x_after(r):
    region = reg(r["C1_NM"])
    if r["C3_NM"] == "계" and r["C4_NM"] == "계":
        return region, "total"
    if r["C4_NM"] == "40㎡이하":
        return region, "small"
    return region, None


TABLES = {
    "permit":   dict(tbl="DT_MLTM_1952", name="인허가", start="200701", cumulative=True,
                     objs=["13102871091A.0001+13102871091A.0002+", "ALL", "ALL", "ALL"], x=x_permit),
    "sale":     dict(tbl="DT_MLTM_5557", name="분양", start="200701", itm="13103133605T1",
                     objs=["ALL", "ALL", "13102133605C.0001+"], x=x_sale, nosize=True),
    "complete": dict(tbl="DT_MLTM_5374", name="준공", start="200701",
                     objs=["ALL", "13102766974B.0001+13102766974B.0002+"], x=x_build("5374")),
    "start":    dict(tbl="DT_MLTM_5388", name="착공", start="200701",
                     objs=["ALL", "13102766970B.0001+13102766970B.0002+"], x=x_build("5388")),
    "unsold":   dict(tbl="DT_MLTM_2080", name="미분양", start="200701", stock=True,
                     objs=["ALL", "ALL", "ALL"], x=x_unsold),
    "unsold_after": dict(tbl="DT_MLTM_5328", name="준공후 미분양", start="200701", stock=True,
                         objs=["ALL", "13102871088B.0001+13102871088B.0002+", "ALL", "ALL"], x=x_after),
}


def build(rows, x):
    res = {}
    seen = set()
    for r in rows:
        region, field = x(r)
        if not region or not field:
            continue
        v = num(r.get("DT"))
        if v is None:
            continue
        k = (r["PRD_DE"], region, field, r.get("C1"), r.get("C2"), r.get("C3"), r.get("C4"))
        if k in seen:
            continue
        seen.add(k)
        slot = res.setdefault(r["PRD_DE"], {}).setdefault(region, {})
        if field == "total":
            slot["total"] = v
        else:
            slot["small"] = slot.get("small", 0) + v
    return res


def decumulate(res):
    out = {}
    for mon in sorted(res):
        y, m = mon[:4], int(mon[4:])
        prev = res.get(f"{y}{m - 1:02d}") if m > 1 else None
        out[mon] = {}
        for region, s in res[mon].items():
            p = (prev or {}).get(region, {})
            cur = {}
            for f, v in s.items():
                if v is None:
                    cur[f] = None
                elif m == 1:
                    cur[f] = v
                else:
                    pv = p.get(f)
                    cur[f] = None if pv is None else v - pv
            out[mon][region] = cur
    return out


def aggregate(res, stock):
    metro = ("서울", "인천", "경기")
    for mon, rg in res.items():
        def add(names, f):
            vals = [rg[n].get(f) for n in names if n in rg]
            if len(vals) < len(names) or any(v is None for v in vals):
                return None
            return sum(vals)
        if "수도권" not in rg and all(n in rg for n in metro):
            rg["수도권"] = {f: add(metro, f) for f in ("total", "small") if any(f in rg[n] for n in metro)}
        if "지방" not in rg and "전국" in rg and "수도권" in rg:
            rg["지방"] = {f: rg["전국"][f] - rg["수도권"][f] for f in ("total", "small")
                         if rg["전국"].get(f) is not None and rg["수도권"].get(f) is not None}
        if "전남광주" not in rg and "광주" in rg and "전남" in rg:
            rg["전남광주"] = {f: add(("광주", "전남"), f) for f in ("total", "small") if f in rg["광주"]}
    return res


def pack(res, spec):
    months = sorted(m for m in res if res[m].get("전국", {}).get("total") is not None)
    regions = [r for r in REGIONS if any(r in res[m] for m in months)]

    def col(f):
        out = {}
        for r in regions:
            out[r] = [None if res[m].get(r, {}).get(f) is None else round(res[m][r][f]) for m in months]
        return out
    d = {"name": spec["name"], "tbl": spec["tbl"], "stock": bool(spec.get("stock")),
         "months": months, "regions": regions, "total": col("total")}
    if not spec.get("nosize"):
        d["small"] = col("small")
        d["smallLabel"] = "40㎡ 이하"
        first = next((m for i, m in enumerate(months) if d["small"]["전국"][i] is not None), None)
        d["smallFrom"] = first
    return d


def main():
    key = api_key()
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))
    end = now.strftime("%Y%m")
    data = {"indicators": {}}
    log = {"checked": now.strftime("%Y-%m-%d %H:%M"), "tables": {}}
    for k, spec in TABLES.items():
        print(f"[수집] {spec['name']} {spec['tbl']}")
        rows = fetch(key, spec["tbl"], spec["objs"], spec["start"], end, spec.get("itm", "ALL"))
        res = build(rows, spec["x"])
        if spec.get("cumulative"):
            res = decumulate(aggregate(res, False))  # 전남광주 첫 달도 전월(광주+전남) 누계로 환산되도록
        res = aggregate(res, spec.get("stock"))
        d = pack(res, spec)
        data["indicators"][k] = d
        last = d["months"][-1] if d["months"] else None
        t = d["total"]["전국"][-1] if last else None
        s = d.get("small", {}).get("전국", [None])[-1] if last else None
        log["tables"][k] = {"tbl": spec["tbl"], "rows": len(rows), "first": d["months"][0] if d["months"] else None,
                            "latest": last, "전국_total": t, "전국_small": s, "smallFrom": d.get("smallFrom")}
        print(f"   rows={len(rows)} {d['months'][0] if d['months'] else '-'}~{last} 전국 전체={t} 40이하={s} 40구분시작={d.get('smallFrom')}")
        if not d["months"]:
            sys.exit(f"[오류] {spec['name']} 데이터가 비었습니다 — 저장하지 않습니다.")

    old = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    if old.get("indicators") == data["indicators"]:
        data["updated"] = old.get("updated")
        print("[완료] 새로 공표된 자료 없음")
    else:
        data["updated"] = now.strftime("%Y-%m-%d %H:%M")
        print("[완료] 데이터 갱신")
    data["checked"] = log["checked"]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    with open(LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
