# -*- coding: utf-8 -*-
"""
Мониторинг цен и скидок продавца Kaspi по нескольким городам.
Запускается по расписанию (GitHub Actions), накапливает историю в data/.

Выход:
  data/latest.csv   — текущий снимок (перезаписывается): по строке на товар×город
  data/changes.csv  — журнал изменений (только когда цена/скидка поменялись)
  data/runs.csv     — сводка по каждому запуску (для графиков трендов)

ENV:
  MERCHANT (по умолчанию Nemo)
  CITIES   (по умолчанию 4 города; формат "код:Имя,код:Имя")
"""
import os, sys, io, csv, time, math, json
from datetime import datetime, timezone
from pathlib import Path
import requests

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

MERCHANT = os.environ.get("MERCHANT", "Nemo")
CITIES_ENV = os.environ.get("CITIES", "").strip()
DEFAULT_CITIES = {
    "750000000": "Алматы",
    "710000000": "Астана",
    "511010000": "Шымкент",
    "470000000": "Актобе",
}
if CITIES_ENV:
    CITIES = dict(part.split(":", 1) for part in CITIES_ENV.split(","))
else:
    CITIES = DEFAULT_CITIES

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)
LATEST  = DATA / "latest.csv"
CHANGES = DATA / "changes.csv"
RUNS    = DATA / "runs.csv"

TS = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def session(city):
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Accept": "application/json, text/*", "Accept-Language": "ru,en;q=0.9",
        "Origin": "https://kaspi.kz",
        "Referer": f"https://kaspi.kz/shop/m/{MERCHANT}/products/",
        "X-KS-City": city,
    })
    return s


def get(s, url, **kw):
    for attempt in range(5):
        try:
            r = s.get(url, timeout=25, **kw)
            if r.status_code == 200:
                return r
        except requests.RequestException:
            pass
        time.sleep(1 + attempt)
    return None


def fetch_city(city):
    """Все товары продавца в одном городе -> список dict."""
    s = session(city)
    q = f":allMerchants:{MERCHANT}"
    r = get(s, "https://kaspi.kz/yml/product-view/pl/filters",
            params={"q": q, "ui": "d", "sort": "relevance", "page": 0, "fl": "true"})
    if not r:
        return []
    d = r.json()["data"]
    qid = d["externalSearchQueryInfo"]["queryID"]
    total = d["total"]
    pages = math.ceil(total / 12) + 3
    seen = {}
    for pg in range(pages):
        r = get(s, "https://kaspi.kz/yml/product-view/pl/results",
                params={"q": q, "ui": "d", "sort": "relevance",
                        "page": pg, "suggestRequestId": qid})
        if not r:
            break
        items = r.json().get("data", [])
        if not items:
            break
        new = 0
        for c in items:
            cid = c.get("id")
            if cid and cid not in seen:
                cats = c.get("category") or c.get("categoryRu") or []
                seen[cid] = {
                    "masterSku": c.get("configSku") or cid,
                    "title": c.get("title", ""),
                    "brand": c.get("brand", ""),
                    "categoryLeaf": cats[-1] if cats else "",
                    "price": c.get("unitSalePrice") or 0,
                    "priceBeforeDiscount": c.get("unitPriceBeforeDiscount") or c.get("unitPrice") or 0,
                    "discount": c.get("discount") or 0,
                }
                new += 1
        if new == 0 and pg > 0:
            break
        time.sleep(0.2)
    return list(seen.values())


def load_prev():
    """Предыдущий снимок: {(masterSku, city): {price, discount}}"""
    prev = {}
    if LATEST.exists():
        with open(LATEST, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                prev[(row["masterSku"], row["city"])] = {
                    "price": row["price"], "discount": row["discount"],
                    "title": row["title"],
                }
    return prev


def main():
    prev = load_prev()
    snapshot = []           # строки latest.csv
    changes  = []           # строки для changes.csv
    run_rows = []           # строки runs.csv

    for code, name in CITIES.items():
        t0 = time.time()
        items = fetch_city(code)
        for it in items:
            key = (str(it["masterSku"]), code)
            snapshot.append({**it, "city": code, "cityName": name})
            # сравнение с прошлым снимком
            old = prev.get(key)
            if old:
                if str(old["price"]) != str(it["price"]):
                    changes.append({"ts": TS, "city": code, "cityName": name,
                                    "masterSku": it["masterSku"], "title": it["title"],
                                    "field": "price", "old": old["price"], "new": it["price"]})
                if str(old["discount"]) != str(it["discount"]):
                    changes.append({"ts": TS, "city": code, "cityName": name,
                                    "masterSku": it["masterSku"], "title": it["title"],
                                    "field": "discount", "old": old["discount"], "new": it["discount"]})
        disc = [int(it["discount"]) for it in items if str(it["discount"]).isdigit() and int(it["discount"]) > 0]
        run_rows.append({"ts": TS, "city": code, "cityName": name,
                         "products": len(items), "discounted": len(disc),
                         "avg_discount": round(sum(disc)/len(disc), 1) if disc else 0})
        print(f"  {name:9} {code}: {len(items)} товаров, со скидкой {len(disc)}, {time.time()-t0:.0f}с")

    # latest.csv — перезапись
    fields = ["masterSku","title","brand","categoryLeaf","city","cityName",
              "price","priceBeforeDiscount","discount"]
    with open(LATEST, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for r in snapshot:
            w.writerow({k: r.get(k, "") for k in fields})

    # changes.csv — дозапись
    ch_fields = ["ts","city","cityName","masterSku","title","field","old","new"]
    new_file = not CHANGES.exists()
    with open(CHANGES, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=ch_fields)
        if new_file: w.writeheader()
        for r in changes:
            w.writerow(r)

    # runs.csv — дозапись
    run_fields = ["ts","city","cityName","products","discounted","avg_discount"]
    new_file = not RUNS.exists()
    with open(RUNS, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=run_fields)
        if new_file: w.writeheader()
        for r in run_rows:
            w.writerow(r)

    print(f"\n{TS}  снимок: {len(snapshot)} строк, изменений: {len(changes)}"
          f"{' (первый запуск — базлайн)' if not prev else ''}")


if __name__ == "__main__":
    main()
