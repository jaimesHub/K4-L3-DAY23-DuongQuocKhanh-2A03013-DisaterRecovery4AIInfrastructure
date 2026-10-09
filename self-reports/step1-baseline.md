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

## S2: Cau hoi 2 — Region B co du lieu hay weights khong

**Cau hoi:** Region B hien tai co du lieu hay model weights khong?

**Dap an:** KHONG. Region B hoan toan trong (empty state).

**Bang chung — curl test:**

```bash
$ curl -s localhost:8002/v1/state
{"region":"b","pool_state":"warm","weights":false,"count":0,"latest_doc_ts":null,"ts":1791524633.903866}

$ curl -s localhost:8002/readyz
{"region":"b","ready":false,"reasons":["pool_state=warm","model_weights_missing","vector_db_empty(count=0)"],"pool_state":"warm","vectors":{"count":0,"latest_doc_ts":null},"ts":1791524636.260617}
```

**Kiem tra file state/region-b:**

```bash
$ ls -la state/region-b/
drwxr-xr-x@ 5 jaimes  staff    160 Oct  9 11:58 .
-rw-r--r--@ 1 jaimes  staff      4 Oct  9 12:03 pool_state      (noi dung: "warm")
-rw-r--r--@ 1 jaimes  staff  16384 Oct  9 11:58 vectors.sqlite  (empty, count=0)
drwxr-xr-x@ 2 jaimes  staff     64 Oct  9 11:58 weights         (khong co model.bin)

$ ls -la state/region-b/weights/
(directory rong)
```

**Ket luan:**
- Region B chi co pool_state file, vector DB (empty), va weights directory (khong co model.bin)
- count = 0 (khong co vector nao)
- weights = false (model.bin khong ton tai)
- pool_state = "warm" (chi chuyen sang "full" khi replicate/failover xong)
- `/readyz` tra 503 vi 3 ly do: pool_state=warm, model_weights_missing, vector_db_empty(count=0)

---

## S3: Cau hoi 3 — Flip active_region sang b

**Cau hoi:** Neu flip `edge/active_region` sang b ngay bay gio, users se nhan gi?

**Doc edge/proxy.py:**
- Line 25-31: `resolve()` - dns cache voi TTL=5s (EDGE_TTL_SECONDS)
- Line 28: Chi doc lai file khi `now - _cache["at"] >= TTL`
- Line 39-43: Goi `/v1/infer` toi upstream va return proxy response voi `edge_region` field

**Thuc nghiem va ket qua:**

Ban dau:
```bash
$ cat edge/active_region
a
```

Flip sang b:
```bash
$ echo b > edge/active_region
```

Sau 7s chờ (TTL=5s):
```bash
$ curl -s -i localhost:8080/v1/infer
HTTP/1.1 503 Service Unavailable
...
{"edge_region":"b","upstream_status":503,"edge_latency_ms":25.7,"error":"region_not_ready","reasons":["pool_state=warm","model_weights_missing","vector_db_empty(count=0)"],"region":"b"}

$ curl -s localhost:8080/edge/state
{"active_region":"b","ttl_seconds":5.0,"cache_age_s":2.7}
```

**Khoi phuc ve a:**
```bash
$ printf a > edge/active_region
$ sleep 7 && curl -s -i localhost:8080/v1/infer
HTTP/1.1 200 OK
...
{"edge_region":"a","upstream_status":200,"edge_latency_ms":30.1,"region":"a","answer":"[a] Ticket #0: khach hoi ve hoa don thang 1, so tien 0d","doc_id":"a-doc-0000",...}

$ cat edge/active_region
a
```

**Ket luan:**
- Khi flip sang b, proxy routing dung sau TTL (5s) expire
- Region B khong co du lieu → HTTP 503 voi reasons chi ra 3 dieu thi thieu
- Khoi phuc ve a tro ve HTTP 200 va dung upstream
- DNS cache (TTL) la yeu to chi ra RTO, users se bi loi trong 5-6 giay sau khi flip

---
