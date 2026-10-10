# Báo cáo BƯỚC 3 — CONTAINMENT

## S1 (3a) — dr/health_checker.py

**Thời gian:** 2026-10-10 21:15 (+07)

### Quyết định thiết kế

- **`probe()` function**: Poll HTTP GET `/readyz` endpoint (KHÔNG `/healthz` vì /healthz chỉ check process còn sống, không check vector DB hay model weights)
- **Timeout bắt buộc**: Dùng `httpx.get(..., timeout=timeout)` để chặn netblock trap (SIGSTOP trong chaos test)
- **Return tuple**: `(ready: bool, reason: str)` để tracking lý do nếu fail
- **Error handling**: Catch `TimeoutException`, `ConnectError`, và generic exceptions để đảm bảo test được monkeypatch

- **`run()` function**: Vòng lặp poll cả 2 region mỗi `interval` giây
  - Trạng thái: HEALTHY (mặc định) → UNHEALTHY (sau >= threshold lần fail liên tiếp)
  - Đếm consecutive failures riêng cho mỗi region
  - **Chỉ ghi JSONL khi state thay đổi** (HEALTHY → UNHEALTHY), KHÔNG ghi mỗi lần poll
  - **Field bắt buộc**: ts, iso (ISO8601), event="state_change", region, to, reason, interval_s, threshold, consecutive_fails
  - Reset counter về 0 khi probe thành công

### Điểm đáng chú ý

1. **Anti-flapping**: Yêu cầu `consecutive_fails >= threshold` để flip state, không phải 1 lần fail
2. **ISO timestamp**: Tính toán từ Unix ts dùng `time.gmtime(ts)` + `time.strftime`
3. **Không import từ serving/**: Hoàn toàn độc lập, chỉ dùng httpx
4. **JSONL append mode**: Mở file với `"a"` (append), không overwrite

### Lệnh test

```bash
pytest tests/test_failover.py::test_health_checker_can_threshold_lien_tiep -v
```

### Output

```
tests/test_failover.py::test_health_checker_can_threshold_lien_tiep PASSED [100%]
```

### Lệch từ CHECKLIST

Không có lệch. Docstring + test + code thật đều khớp.

---

## S2 (3b) — dr/failover.py

**Thời gian:** 2026-10-10 21:20 (+07)

### Quyết định thiết kế

- **`emit(**kw)` function**: 
  - Thêm ts (Unix timestamp) + iso (ISO8601) vào kw
  - Ghi JSONL vào reports/failover-events.jsonl (append mode)
  - In stdout để xem progress
  - Mkdir reports nếu chưa tồn tại

- **`failover(target, backend, wait)` — 5 bước:**
  
  1. **1_verify_target**: HTTP GET /v1/state, emit với pool_state, vector_count, weights
  
  2. **2_restore_snapshot**: 
     - Gọi `snapshot.get(target, backend)` để restore weights + vectors
     - Gọi `snapshot.rpo()` để tính rpo_seconds + docs_lost
     - Emit với rpo_seconds, docs_lost, embed_model_version (BẮT BUỘC cho measure_rto.py)
  
  3. **3_scale_pool**: 
     - Ghi "full\n" vào state/region-<target>/pool_state
     - Mkdir parents=True để tạo thư mục nếu cần
     - Emit với to="full"
  
  4. **4_wait_ready**:
     - Vòng lặp tối đa `wait` giây, poll /readyz mỗi 0.5s
     - Nếu HTTP 200 + ready=true → thành công
     - Nếu timeout → ABORT (không cắt DNS), return {ok: false, reason: "target_timeout"}
     - Emit dù success hay timeout
  
  5. **5_dns_cutover**:
     - Chỉ chạy nếu step 4 thành công
     - Ghi target vào edge/active_region
     - Emit step=5_dns_cutover

- **Error handling**: Try-catch toàn bộ để return {ok: false, reason: ...} nếu fail

### Điểm đáng chú ý

1. **5 bước PHẢI theo thứ tự** — snapshot.rpo() dùng primary_db (state/region-a) và restored_db (state/region-<target>)
2. **Step 4 timeout → abort**: Nếu target không ready trong `wait` giây, KHÔNG cutover DNS
3. **rpo_seconds + docs_lost** bắt buộc — tools/measure_rto.py đọc từ step 2_restore_snapshot
4. **Emit **trước** các thay đổi file (scale_pool, cutover)** để log có ts chính xác

### Lệnh test

```bash
pytest tests/test_failover.py::test_failover_khong_cutover_khi_target_chua_ready -v
```

### Output

```
tests/test_failover.py::test_failover_khong_cutover_khi_target_chua_ready PASSED [100%]
```

### Lệch từ CHECKLIST

Không có lệch. Code khớp docstring + test.

---

## S3 (3c) — dr/runbook.py

**Thời gian:** 2026-10-10 21:30 (+07)

### Quyết định thiết kế

- **`step(n, name, **kw)` function**:
  - Ghi JSONL vào reports/runbook-run.jsonl (append mode)
  - Thêm ts (Unix), iso (ISO8601), step (n), name, + các kw
  - Print stdout để xem progress
  - Mkdir reports nếu chưa tồn tại

- **`confirm(auto, msg)` function**:
  - Nếu auto=True → return True (để CI/--auto chạy tự động)
  - Nếu auto=False → hỏi user: input("msg (y/N): "), return True nếu gõ 'y'
  - Bán tự động: operator phải confirm trước failover

- **`run(primary, target, backend, auto)` — 7 bước:**
  
  1. **xac_nhan_outage**: Probe primary 3 lần, phải fail tất cả
  2. **thong_bao_incident**: Ghi ts, lấy t_outage_from_chaos từ chaos-events.jsonl nếu có
  3. **scale_gpu_pool**: Gọi failover.failover(...) MỘT LẦN DUY NHẤT, emit kết quả (ok + reason)
  4. **verify_state_replica**: Đọc state/region-<target>/vectors.sqlite + weights, emit vector_count + weights_restored (KHÔNG gọi lại failover)
  5. **dns_cutover**: Đọc edge/active_region, verify = target (KHÔNG gọi lại failover)
  6. **verify_golden_signals**: 10 request thật vào http://127.0.0.1:8080/v1/infer, tính p95_ms (95th percentile) + error_rate
  7. **post_incident**: Tính elapsed_s từ step 2, log guide đo RTO

### Điểm đáng chú ý

1. **Failover gọi MỘT LẦN DUY NHẤT** ở step 3 (bước scale_gpu_pool)
2. **Bán tự động**: Mặc định hỏi confirm, --auto bỏ qua hỏi
3. **Step 4-5 chỉ ĐỌC**: Không gọi lại failover, chỉ verify kết quả đã xảy ra
4. **Golden signals**: 10 request, tính p95 + error rate, không là đo throughput

### Lệnh test

```bash
pytest tests/test_failover.py -v
```

### Output

```
tests/test_failover.py::test_rpo_dem_dung_so_doc_bi_mat PASSED           [ 33%]
tests/test_failover.py::test_health_checker_can_threshold_lien_tiep PASSED [ 66%]
tests/test_failover.py::test_failover_khong_cutover_khi_target_chua_ready PASSED [100%]

============================== 3 passed in 0.33s ===============================
```

### Lệch từ CHECKLIST

Không có lệch. Code khớp docstring.

