# Buoc 1 — Baseline Self-Report

Thoi diem: 2026-10-09 05:43 (+07)

---

## S1: Cau hoi 1 — Phat hien Region A chet

**Cau hoi:** Neu Region A chet ngay bay gio, component nao se phat hien — va bang cach nao?

**Dap an:** Hien tai KHONG CO component nao phat hien tu dong. Cac component (health_checker.py, runbook.py) se duoc them o Buoc 3. Tuy nhien, co the POLL `/readyz` endpoint (KHONG phai `/healthz`) de phat hien.

**Ly do:**

`/healthz` endpoint (serving/app.py line 56-59) chi kiem tra xem process con song hay khong:
```
@app.get("/healthz")
def healthz():
    """Liveness: process con song. KHONG kiem tra state -> dung failover dua vao cai nay."""
    return {"region": REGION, "alive": True, "ts": time.time()}
```

Nhan xet: Chi tra ve `alive:true` neu process con chay, khong kiem tra du lieu hay moi weights.

`/readyz` endpoint (serving/app.py line 62-80) kiem tra 4 dieu kien:
1. Pool state phai la "full" (line 69)
2. Warm-up time con lai phai la 0 (line 71)
3. Model weights phai ton tai (line 73)
4. Vector DB count >= MIN_VECTORS (line 75)

Co 4 dieu kien trong `reasons` list, va status code 503 neu co bat ki reason nao, 200 neu ready (line 79).

**Bang chung — curl test:**

```bash
$ curl -s -i localhost:8001/healthz
HTTP/1.1 200 OK
...
{"region":"a","alive":true,"ts":1791524601.210434}

$ curl -s -i localhost:8001/readyz
HTTP/1.1 200 OK
...
{"region":"a","ready":true,"reasons":[],"pool_state":"full","vectors":{"count":200,"latest_doc_ts":1791522189.326974},"ts":1791524603.6415842}
```

**Ket luan:**
- `/healthz` chi noi process con song, KHONG noi region co san sang phuc vu traffic
- `/readyz` noi region san sang phuc vu traffic — co day du du lieu, moi weights, pool warm-up hoan thanh
- De phat hien outage, nen POLL `/readyz`, khong phai `/healthz`

---
