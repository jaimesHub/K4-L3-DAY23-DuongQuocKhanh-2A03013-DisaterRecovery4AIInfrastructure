# Buoc 1 — Baseline Self-Report

Thoi diem: 2026-10-09 12:43 (+07)

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

## S4: Doc ki 3 file

### serving/app.py

**File:** serving/app.py

**Tom tat:** 
- Line 21: `WARMUP = float(os.environ.get("WARMUP_SECONDS", "6"))` - warm-up duration = 6 giay
- Line 56-59: `/healthz` endpoint - chi kiem tra process con song
- Line 62-80: `/readyz` endpoint - dieu kien (4 conditions):

**Chi tiet /readyz (lines 62-80):**
1. **Condition 1 (line 69-70):** pool_state phai = "full" 
   - `if ps != "full": reasons.append(f"pool_state={ps}")`
2. **Condition 2 (line 71-72):** warm-up time con lai phai = 0
   - `warm_left = max(0.0, (_scale["since"] + WARMUP) - time.time())`
   - `if warm_left > 0: reasons.append(f"warming_up_{warm_left:.1f}s_left")`
3. **Condition 3 (line 73-74):** model.bin phai ton tai
   - `if not weights_ok(): reasons.append("model_weights_missing")`
4. **Condition 4 (line 75-76):** vector count >= MIN_VECTORS (default 1)
   - `if v["count"] < MIN_VECTORS: reasons.append(f"vector_db_empty(count={v['count']})")`
5. **Status code (line 79):** HTTP 200 neu no reasons, 503 neu co bat ki reason nao
   - `response.status_code = 200 if not reasons else 503`

**Nhan xet:** warm-up = 6 giay, va chi dung khi pool_state transition tu warm → full luc chay (line 36: `_scale["since"] = 0.0 if first else time.time()`).

---

### edge/proxy.py

**File:** edge/proxy.py

**Tom tat:**
- Line 16: `TTL = float(os.environ.get("EDGE_TTL_SECONDS", "5"))` - cache TTL = 5 giay
- Line 25-31: `resolve()` - mô phỏng DNS cache

**Chi tiet resolve() (lines 25-31):**
```python
def resolve() -> str:
    """Gia lap DNS cache: chi doc lai file sau khi TTL het han."""
    now = time.time()
    if _cache["region"] is None or now - _cache["at"] >= TTL:
        _cache["region"] = ACTIVE_FILE.read_text().strip() if ACTIVE_FILE.exists() else "a"
        _cache["at"] = now
    return _cache["region"]
```
- Neu `_cache["region"]` chua set hoac TTL het, chi doc lai file `edge/active_region`
- Neu TTL chua het, tra ve gia tri cache (khong doc lai file)
- Day la yeu to chi ra TTL cache cong them giay vao RTO

**Nhan xet:** Sau khi flip edge/active_region, proxy se chuyen routing sau TTL (5s). Neu TTL=5s, RTO se tang them 5+ giay.

---

### state/snapshot.py

**File:** state/snapshot.py

**Tom tat:**
- Line 47-64: `rpo()` function - tinh toan RPO (thoi gian + so doc bi mat)
- Line 67-82: `put()` - snapshot vectors + weights (vao disk hoac S3)
- Line 85-107: `get()` - restore vectors + weights tu snapshot

**Chi tiet rpo() (lines 47-64):**
```python
def rpo(primary_db: pathlib.Path, restored_db: pathlib.Path) -> dict:
    a_ts, b_ts = latest_doc_ts(primary_db), latest_doc_ts(restored_db)
    lost = None
    if b_ts is not None and primary_db.exists():
        con = sqlite3.connect(f"file:{primary_db}?mode=ro", uri=True)
        try:
            lost = con.execute("SELECT COUNT(*) FROM docs WHERE ingested_at > ?", (b_ts,)).fetchone()[0]
        finally:
            con.close()
    return {"primary_latest_doc_ts": a_ts, "restored_latest_doc_ts": b_ts,
            "rpo_seconds": None if (a_ts is None or b_ts is None) else round(a_ts - b_ts, 2),
            "docs_lost": lost}
```
- **rpo_seconds (line 63):** `round(a_ts - b_ts, 2)` - khoang thoi gian tu doc moi nhat trong restored den primary
- **docs_lost (line 59):** So document voi `ingested_at > restored_latest_doc_ts` - so document bi mat (con so khach hang quan tam)

**Nhan xet:** RPO = time gap giua primary_latest va restored_latest. Failover se mat du lieu trong thoi gian nay.

---

## Tong ket Buoc 1

| Cau hoi | Dap an |
|--------|--------|
| 1. Ai phat hien region chung? | Hien tai khong ai; should poll /readyz endpoint (khong /healthz). /readyz kiem tra 4 dieu kien: pool_state=full, warmup=0, weights=true, vectors>0 |
| 2. Region B co du lieu? | Khong. count=0, weights=false, pool_state=warm. /readyz tra 503. |
| 3. Neu flip sang b ngay bay gio? | HTTP 503 (3 ly do). DNS cache TTL=5s, routing change sau >6s. |

**Key takeaway cho Buoc 2-3:**
- Health checker se poll /readyz moi 5s, threshold 3 lan fail lien tiep = detect floor 15s
- TTL cache 5s → cong them giay vao RTO
- Warm-up 6s → phai chop pool_state=full, sau do chop 6s warm-up time
- RPO = time gap giua snapshot va primary, documents bi mat trong gap nay
- Failover = restore snapshot + warm pool + cutover DNS
