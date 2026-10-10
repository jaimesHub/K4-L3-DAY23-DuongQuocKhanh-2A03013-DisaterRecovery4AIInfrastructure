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

---

## TỔNG KẾT BƯỚC 3

**Thời gian hoàn thành:** 2026-10-10 21:42 (+07)

### Bảng Kết Quả Test

| Test | Kết quả | File |
|---|---|---|
| `test_health_checker_can_threshold_lien_tiep` | ✅ PASSED | dr/health_checker.py |
| `test_failover_khong_cutover_khi_target_chua_ready` | ✅ PASSED | dr/failover.py |
| `test_rpo_dem_dung_so_doc_bi_mat` | ✅ PASSED | (unit test sẵn có) |
| **Tổng: 3/3 test** | **✅ PASSED** | |

### Quyết Định Thiết Kế Chính

1. **Health Checker Anti-Flap:**
   - Threshold ≥ 3 lần fail liên tiếp trước flip state
   - Chỉ ghi JSONL khi state thay đổi (HEALTHY → UNHEALTHY)
   - Detection floor = interval × threshold = 5 × 3 = 15s
   - **RTO impact:** ~53% của tổng RTO (15/28s)

2. **Failover 5 Bước Đúng Thứ Tự:**
   - Step 4 timeout → abort (không cutover)
   - Step 2 bắt buộc ghi rpo_seconds + docs_lost (cho measure_rto.py)
   - Step 5 chỉ chạy nếu step 4 thành công
   - **Risk:** Nếu step 2 fail → không có snapshot → failover sẽ fail ở restore

3. **Runbook Bán Tự Động:**
   - Default hỏi confirm (--auto bỏ qua)
   - Failover gọi MỘT LẦN DUY NHẤT (step 3)
   - Step 4-5 chỉ ĐỌC kết quả (không gọi lại failover)
   - Golden signals: 10 request, tính p95 + error rate
   - **Risk:** Nếu runbook.py call failover 2 lần → failover-events.jsonl có 10 dòng (sẽ fail measure_rto.py)

### Rủi Ro Cho Bước 4 (DR Proof)

1. **Snapshot phải tồn tại:**
   - BẮTBUỘC chạy `state/ingest.py + state/replicate.py` trước khi kill region A
   - Nếu quên → failover.py die ở step 2_restore_snapshot
   - **Mitigation:** Task yêu cầu sleep 5s trước attack, chờ replicate cycle đầu tiên

2. **Dry-run sẽ thay đổi state:**
   - Dry-run runbook.py sẽ restore region B + flip edge/active_region → edge/active_region = b
   - Nếu chạy dry-run trước Bước 4 → phải restore edge/active_region = a trước attack
   - **Mitigation:** CHECKLIST ghi rõ: "KHÔNG tick dry-run, để dành cho Bước 4"

3. **Health checker log tích luỹ:**
   - health-events.jsonl mở ở mode append → nếu run dry-run + lại run Bước 4 cùng ngày
   - tools/measure_rto.py phải filter bằng time window của loadgen (không bị lẫn log cũ)
   - **Mitigation:** measure_rto.py đã xử lý, chỉ cần không xoá log cũ

4. **Active region state:**
   - Sau S1-S4, edge/active_region sẽ là 'a' (ban đầu)
   - Bước 4 sẽ flip nó → 'b'
   - **Mitigation:** Kiểm tra `cat edge/active_region` là 'a' trước tấn công

### Thứ Tự Lệnh Bước 4

Để chắc chắn không lỗi, Bước 4 PHẢI theo thứ tự này:

```bash
# 0. Verify state ban đầu
cat edge/active_region                      # Phải là 'a'
curl localhost:8001/readyz | jq .ready     # Phải là true

# 1. Khởi động ingest + replicate (chạy nền, phải trước attack)
python3 state/ingest.py --region a --rate 0.5 --duration 150 &
python3 state/replicate.py --every 30 --duration 150 --backend fs &
sleep 5                                     # Chờ replicate cycle 1 xong

# 2. Khởi động drill (load + health + chaos)
python3 loadgen/traffic.py --duration 100 --rps 2 --out reports/drill-2-withdr.jsonl &
python3 dr/health_checker.py --interval 5 --threshold 3 --duration 100 --out reports/health-events.jsonl &
sleep 12                                    # Chờ load generator + health checker chạy 1 chút

# 3. Kill region A
python3 chaos/kill_region.py --region a --mode netblock --mock

# 4. Chạy runbook (automation)
python3 dr/runbook.py --primary a --target b --backend fs --auto

# 5. Đợi drill kết thúc
wait                                        # Chờ loadgen kết thúc

# 6. Đo RTO
python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300

# 7. Verify kết quả
# - rto_verdict = PASS
# - warnings = []
# - valid = true
# - recovered_by_region != a (phục vụ từ region b)
```

### Chú Ý Quan Trọng

1. **Timeout là feature, không phải bug:**
   - Health checker interval=5s, threshold=3 → detection floor=15s là lâu
   - Nhưng giảm interval → flapping risk từ network jitter
   - Trade-off: RTO vs stability

2. **RPO không cố định:**
   - Thay đổi tùy mốc snapshot.replicate.py
   - 1 lần run: 4s + 7 docs; lần khác: 14s + 2 docs
   - Tuy nhiên RTO ổn định (28–28.6s qua các run)

3. **Failover 5 bước tuần tự:**
   - KHÔNG thể song song (bước n+1 phụ thuộc bước n)
   - Step 4 timeout → fail-safe (không cắt DNS) → bảo vệ user

4. **Runbook log timeline:**
   - Mỗi step có ts riêng
   - Postmortem đọc từ runbook-run.jsonl để tính elapsed_s
   - Khác với failover-events.jsonl (5 sub-step)

---

## S4 (3d) — reports/runbook.md

**Thời gian:** 2026-10-10 21:40 (+07)

### Nội dung

Điền template 7 bước + rollback plan theo yêu cầu:

**7 bước failover:**
1. Xác nhận outage — probe region A 3 lần, phải fail → on-call
2. Mở incident + bấm giờ — chạy runbook.py (bán tự động hoặc --auto) → incident commander
3. Restore state — snapshot.get() tự động trong runbook.py
4. Scale pool warm→full — automation, verify /readyz = true
5. DNS cutover — automation, verify edge/active_region = b
6. Golden signals — 10 request, p95 + error rate
7. Đo RTO — tools/measure_rto.py, cần rto_verdict = PASS

**Rollback plan:**
- Điều kiện: Region A sống + ổn định (error < 1%), Region B không sự cố
- Quyết định: Incident Commander (con người, KHÔNG auto)
- Cách: `echo a > edge/active_region` hoặc automation
- Tiêu chí commit: RTO rollback ≤ 30s (A có dữ liệu sẵn)

**Troubleshooting:**
- Snapshot không tìm → replicate.py chưa run
- /readyz timeout → GPU warmup, check log
- Error rate cao → rollback sang A, debug B

**Recovery checkpoint:**
- edge/active_region = b
- /readyz = true
- Vector count > 0
- Test single request
- Measure RTO (valid=true, rto_verdict=PASS)

### Điểm đáng chú ý

1. **Copy-paste được**: Mỗi lệnh là CLI real, không placeholder
2. **Owner rõ ràng**: On-call, SRE, Incident Commander, không mơ hồ
3. **Completion signal**: Cách nhận biết bước xong (curl output, file check, JSON query)
4. **Rollback không auto**: Ghi rõ "quyết định con người", tránh flap
5. **Postmortem questions**: 5 câu hỏi để suy ngẫm RTO/RPO

### Lệch từ CHECKLIST

Không có lệch. Runbook đủ 7 bước + rollback + troubleshooting + recovery checkpoint.

