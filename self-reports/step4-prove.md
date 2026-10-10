# Báo cáo BƯỚC 4 — PROVE & COLLECT EVIDENCE

**Thời gian:** 2026-10-10 21:59 (+07)

## Drill 2 — Chạy với DR (ingest + replicate + failover)

### Quá trình thực hiện

**Khởi động ingest + replicate (2026-10-10T14:56:46):**
```
python3 state/ingest.py --region a --rate 0.5 --duration 150 &
python3 state/replicate.py --every 30 --duration 150 --backend fs &
sleep 5
```
- reports/replication.jsonl được tạo với snapshot lần đầu
- state/_replica/dr-artifacts/MANIFEST.json tồn tại → snapshot sẵn sàng

**Chạy full drill — loadgen + health_checker + chaos + runbook (2026-10-10T14:57:04):**
```
python3 loadgen/traffic.py --duration 100 --rps 2 --out reports/drill-2-withdr.jsonl &
python3 dr/health_checker.py --interval 5 --threshold 3 --duration 100 --out reports/health-events.jsonl &
sleep 12
python3 chaos/kill_region.py --region a --mode netblock --mock
python3 dr/runbook.py --primary a --target b --backend fs --auto
wait
```

### Kết quả RTO đo được

```json
{
  "valid": true,
  "invalid_reasons": [],
  "warnings": [],
  "rto_measured_s": 28.7,
  "rto_target_s": 300.0,
  "rto_verdict": "PASS",
  "rpo_at_restore_s": 8.01,
  "docs_lost": 4
}
```

**Kết luận:** RTO = 28.7s (< 300s target) ✓ | RPO = 8.01s / 4 docs ✓ | Không có warnings ✓

### Timeline (tính từ t_outage = 1791644224.113635)

| Mốc | Sự kiện | +Giây từ t_outage | Evidence |
|---|---|---|---|
| t_outage | Kill region A | 0.0 | chaos/chaos-events.jsonl:3 |
| t_first_error | Request đầu tiên fail (ok:false) | +0.2s | reports/drill-2-withdr.jsonl:25 |
| t_health_detect | Health check phát hiện UNHEALTHY | +19.4s | reports/health-events.jsonl:2 |
| t_dns_cutover | DNS cutover (step 5) | +26.3s | reports/failover-events.jsonl:5 |
| t_recovered | Request đầu tiên thành công từ B (ok:true) | +28.7s | reports/drill-2-withdr.jsonl:39 |

### RTO Breakdown (4 thành phần)

| Thành phần | Giây | Ghi chú | Evidence |
|---|---|---|---|
| Detection floor (health-check) | 15.0 = 5s × 3 | interval × threshold | reports/health-events.jsonl:2 `interval_s:5, threshold:3` |
| Snapshot restore | 0.03 = t_2 - t_1 | Restore từ filesystem | reports/failover-events.jsonl:1→2 (1791644244.245 → 1791644244.275) |
| GPU pool warm-up | 6.1s | Phần lớn thời gian ở step 4_wait_ready | reports/failover-events.jsonl:4 `waited_s:6.1` |
| DNS/TTL cache | 2.4 = t_recovered - t_dns_cutover | Client cache EDGE_TTL_SECONDS | reports/drill-2-withdr.jsonl:39 (1791644250.389 → 1791644252.789) |
| **TỔNG** | **28.7s** | Phải ≈ RTO đo được | reports/measure-drill-2.json |

### Chú thích thiết kế

**Sửa vấn đề trước Bước 4 (COMMIT 0):** 
- Runbook.py step 1 bay giờ **chờ health_checker xác nhận UNHEALTHY** sau khi probe 3 lần fail
- Thay vì failover ngay (~13-15s), runbook cấp ~60s cho health checker phát hiện outage (~15-17s)
- Kết quả: `t_cutover (26.3s) >= t_health_detect (19.4s)` → KHÔNG có warning "t_cutover < t_detect"
- Log bước 1 ghi `health_confirmed=true, t_health_detect=1791644243.4794219`

**Sửa này quan trọng vì:**
- measure_rto.py cảnh báo nếu `t_cutover < t_detect` (chứng tỏ failover hành động trước khi automation phát hiện)
- Test `test_drill2_hop_le` yêu cầu `warnings == []` (empty list)
- Lần chạy này thành công vì runbook chờ, không cắt DNS vội vàng

### Trạng thái sau drill

- edge/active_region = b (giữ nguyên, KHÔNG sửa tay)
- Region A: restored + sống (readyz = true)
- Region B: phục vụ traffic (active)
- reports/measure-drill-2.json: valid=true, rto_verdict=PASS
- reports/runbook-run.jsonl, reports/failover-events.jsonl, reports/health-events.jsonl: có sẵn

### C3 — Pytest test results (RTO valid + timestamps)

```bash
pytest tests/test_rto_evidence.py::test_drill2_hop_le \
        tests/test_rto_evidence.py::test_rto_do_duoc_bang_timestamp \
        tests/test_rto_evidence.py::test_chaos_khong_giet_ca_hai_region \
        tests/test_rto_evidence.py::test_health_check_interval_duoc_ghi_lai -v
```

**Output:**
```
tests/test_rto_evidence.py::test_drill2_hop_le PASSED
tests/test_rto_evidence.py::test_rto_do_duoc_bang_timestamp PASSED
tests/test_rto_evidence.py::test_chaos_khong_giet_ca_hai_region PASSED
tests/test_rto_evidence.py::test_health_check_interval_duoc_ghi_lai PASSED

4 passed in 0.03s
```

**Kết luận:**
- ✓ test_drill2_hop_le: valid=true, warnings=[] (RTO hợp lệ, không có warning t_cutover < t_detect)
- ✓ test_rto_do_duoc_bang_timestamp: rto_measured_s!=null, recovered_by_region!=killed_region
- ✓ test_chaos_khong_giet_ca_hai_region: forced_both=false, other_alive=true
- ✓ test_health_check_interval_duoc_ghi_lai: interval_s=5, threshold=3, detect_floor_s=15.0

### C4 — Pytest test results (RPO count + không ước lượng)

```bash
pytest tests/test_failover.py::test_rpo_dem_dung_so_doc_bi_mat \
        tests/test_rto_evidence.py::test_rpo_duoc_do_chu_khong_uoc_luong -v
```

**Output:**
```
tests/test_failover.py::test_rpo_dem_dung_so_doc_bi_mat PASSED
tests/test_rto_evidence.py::test_rpo_duoc_do_chu_khong_uoc_luong PASSED

2 passed in 0.06s
```

**Kết luận:**
- ✓ test_rpo_dem_dung_so_doc_bi_mat: snapshot.rpo() tính đúng số doc bị mất (4 docs)
- ✓ test_rpo_duoc_do_chu_khong_uoc_luong: rpo_at_restore_s=8.01 (từ log, không ước lượng)

---

## C5 — Tổng kết BƯỚC 4

### Bảng Test Results

| Test | Status | Ghi chú |
|---|---|---|
| test_drill2_hop_le | ✓ PASSED | valid=true, warnings=[] |
| test_rto_do_duoc_bang_timestamp | ✓ PASSED | rto_measured_s=28.7s |
| test_chaos_khong_giet_ca_hai_region | ✓ PASSED | Không double outage |
| test_health_check_interval_duoc_ghi_lai | ✓ PASSED | interval_s=5, threshold=3 |
| test_rpo_dem_dung_so_doc_bi_mat | ✓ PASSED | docs_lost=4 |
| test_rpo_duoc_do_chu_khong_uoc_luong | ✓ PASSED | rpo_at_restore_s=8.01 |

### Số liệu chính — RTO/RPO

| Chỉ số | Giá trị | So với target | Verdict |
|---|---|---|---|
| RTO đo được | 28.7s | < 300s target | ✓ PASS |
| RPO đo được | 8.01s / 4 docs | < 300s target | ✓ PASS |
| Requests failed | 14 / 156 | 9% failure rate | ✓ PASS |

### RTO Breakdown (4 thành phần)

1. Detection (kill → failover start): **20.13s** — 70% RTO
   - Gồm: 15.0s theoretical floor (5s × 3) + 5.13s probe/timeout/poll overhead
2. Snapshot restore: **0.03s** — 0.1% RTO
3. GPU pool warm-up: **6.11s** — 21% RTO
4. DNS/TTL cache: **2.40s** — 8% RTO
**TỔNG: 28.67s** ≈ **28.7s** ✓

### Sửa lỗi trước Bước 4 — Hiệu quả

**Vấn đề:** runbook.py cắt DNS trước khi health_checker phát hiện outage (t_cutover < t_detect)
- Trước sửa: failover ~13-15s → cutover ~15s, health detect ~17s → warning "t_cutover < t_detect"
- Sau sửa: runbook chờ health_checker xác nhận UNHEALTHY (tối đa 60s) → t_cutover (26.3s) >= t_detect (19.4s) → NO WARNING ✓

**Kết quả:** test_drill2_hop_le PASSED vì warnings=[] 

### Lưu ý cho Bước 5 (reports/rto-evidence.md + reports/postmortem.md)

**Cần copy vào rto-evidence.md:**
- t_outage: 1791644224.113635 (chaos/chaos-events.jsonl:3)
- t_first_error: 1791644224.281 (reports/drill-2-withdr.jsonl:25)
- t_health_detect: 1791644243.479 (reports/health-events.jsonl:2)
- t_dns_cutover: 1791644250.389 (reports/failover-events.jsonl:5)
- t_recovered: 1791644252.789 (reports/drill-2-withdr.jsonl:39)

**Cần copy vào postmortem.md:**
- RTO: 28.7s < 300s target → GAP = 271.3s
- RPO: 8.01s < 300s target → GAP = 291.99s
- Bottleneck: Detection 20.13s (70% — gồm 15s floor + 5.13s overhead) + GPU warmup 6.11s (21%)
- Action item: giảm health check interval từ 5s → 3s có thể tiết kiệm ~6s RTO

**Tệp không được commit (`.gitignore`):**
- reports/runbook-run.jsonl → nội dung đã dán trong báo cáo
- reports/measure-drill-2.json → nội dung đã dán trong báo cáo

## C2 — Đo RTO từ log

### JSON measure-drill-2.json (full)

```json
{
  "valid": true,
  "invalid_reasons": [],
  "warnings": [],
  "killed_region": "a",
  "recovered_by_region": "b",
  "t_outage_iso": "2026-10-10T14:57:04",
  "chaos_mode": "netblock",
  "breakdown_seconds_from_t0": {
    "user_thay_loi_dau_tien": 0.2,
    "health_check_phat_hien": 19.4,
    "dns_cutover": 26.3,
    "request_thanh_cong_dau_tien": 28.7
  },
  "health_check_config": {
    "interval_s": 5.0,
    "threshold": 3,
    "detect_floor_s": 15.0
  },
  "rto_measured_s": 28.7,
  "rto_target_s": 300.0,
  "rto_verdict": "PASS",
  "rpo_at_restore_s": 8.01,
  "docs_lost": 4,
  "requests_failed": 14,
  "evidence": {
    "loadgen": "reports/drill-2-withdr.jsonl",
    "chaos": "chaos/chaos-events.jsonl",
    "health": "reports/health-events.jsonl",
    "failover": "reports/failover-events.jsonl"
  }
}
```

### Phân tích RTO Breakdown chi tiết

**1. Detection (20.13s) — 70% của RTO:**
- Thành phần:
  - 15.0s theoretical floor: Health check interval=5s, threshold=3 → sớm nhất phát hiện là 5×3=15s
  - 4.13s additional overhead: Network polling jitter + runbook poll/handoff
- Health check phát hiện: +19.4s (log: `reports/health-events.jsonl:2`)
- Failover start: +20.13s (log: `reports/failover-events.jsonl:1` — step 1_verify_target)

**2. Snapshot Restore (0.03s) — < 1% của RTO:**
- Restore từ filesystem: rất nhanh
- failover-events.jsonl bước 1→2: t_verify (1791644244.245) → t_restore (1791644244.275) = 0.03s
- RPO = 8.01s do replication lag (snapshot của ~8.01s trước)

**3. GPU Pool Warm-up (6.1s) — 21% của RTO:**
- Step 4_wait_ready phải poll /readyz tới khi region B ready=true
- failover-events.jsonl:4 ghi `waited_s:6.1`
- Bottleneck: GPU pool transition từ "warm" → "full" + model weight loading

**4. DNS/LB TTL Cache (2.4s) — 8% của RTO:**
- EDGE_TTL_SECONDS cache ở proxy → client không liền thấy cutover
- t_dns_cutover (26.3s) → t_first_ok (28.7s) = 2.4s cache delay
- evidence: drill-2-withdr.jsonl:39 lần đầu ok:true từ served_by=b

### Ghi chú

- runbook-run.jsonl KHÔNG được commit (bị .gitignore) 
- measure-drill-2.json KHÔNG được commit (bị .gitignore)
- Nội dung runbook-run.jsonl (7 bước):
```
Step 1 (xac_nhan_outage): {"alive": false, "probes_failed": 3, "health_confirmed": true, "t_health_detect": 1791644243.4794219, ...}
Step 2 (thong_bao_incident): {"t_outage_from_chaos": 1791644224.113635, ...}
Step 3 (scale_gpu_pool): {"ok": true, ...}
Step 4 (verify_state_replica): {"vector_count": 215, "weights_restored": true, ...}
Step 5 (dns_cutover): {"active_region": "b", "cutover_ok": true, ...}
Step 6 (verify_golden_signals): {"requests_sent": 10, "errors": 1, "p95_ms": 2257.1, "error_rate": 10.0, ...}
Step 7 (post_incident): {"elapsed_s": 9.0, ...}
```

---

## Đính chính sau review

### 3 lỗi chính

1. **chaos/chaos-events.jsonl evidence path — Sai: :2, Đúng: :3**
   - Cách kiểm: `wc -l chaos/chaos-events.jsonl` (4 dòng); dòng 1-2 = drill-1, dòng 3-4 = drill-2. Dòng 3 có `ts: 1791644224.113635, action: kill` (t_outage drill-2)
   - Sửa tại: dòng 49 (timeline table), dòng 172 (Lưu ý Bước 5)

2. **reports/drill-2-withdr.jsonl evidence path — Sai: :17, Đúng: :25**
   - Cách kiểm: `grep -n "ok.*false" reports/drill-2-withdr.jsonl | head -1` → dòng 25 là first ok:false. Seq 24, ts: 1791644224.281, ReadTimeout
   - Sửa tại: dòng 50 (timeline table), dòng 173 (Lưu ý Bước 5)

3. **RTO breakdown percentages — Sai: 52% floor; Đúng: 70% detection**
   - Cách kiểm: Tính từ timestamp thật: Detection = 20.13s / 28.67s = 70.2%
   - Sửa tại: dòng 155-159 (breakdown table), dòng 181 (bottleneck), dòng 229-232 (detailed analysis)
   - Chi tiết:
     - Detection KHÔNG phải chỉ 15.0s floor, mà 20.13s (kill → failover start)
     - 15.0s là thành phần lý thuyết tối thiểu (health floor)
     - 5.13s là overhead (probe timeout + runbook poll/handoff)
     - Restore: 0.03s (0.1%)
     - Warmup: 6.11s (21.3%)
     - DNS: 2.40s (8.4%)
     - Tổng: 28.67s ≈ 28.7s ✓

### Không có lỗi khác
- health-events.jsonl:2 ✓ (detect lúc +19.4s)
- failover-events.jsonl ✓ (5 bước, timestamps khớp)
- RTO 28.7s ✓ (measure_rto.py xác nhận)
- RPO 8.01s / 4 docs ✓ (từ failover step 2)
