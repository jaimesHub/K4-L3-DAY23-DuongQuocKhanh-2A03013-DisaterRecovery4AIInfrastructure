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

