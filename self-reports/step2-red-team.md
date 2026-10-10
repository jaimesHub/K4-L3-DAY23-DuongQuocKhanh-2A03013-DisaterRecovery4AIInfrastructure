# Bước 2 — RED TEAM (Chứng minh baseline không tự phục hồi)

## S1. Chạy baseline drill (2026-10-10 lúc 21:00 +07)

**Thời gian thực hiện:** Sat Oct 10 21:00:31 +07 2026

**Bối cảnh:** Chạy load generator 40 giây, 2 RPS; sau 8 giây kill region A bằng SIGSTOP (netblock --mock)

**Lệnh chạy:**
```bash
python3 loadgen/traffic.py --duration 40 --rps 2 --out reports/drill-1-nodr.jsonl &
sleep 8
python3 chaos/kill_region.py --region a --mode netblock --mock
wait
```

**Output từ kill_region:**
```
CHAOS {"ts": 1791640848.3269491, "iso": "2026-10-10T14:00:48", "action": "kill", "region": "a", "mode": "netblock", "backend": "bare", "mock": true, "other_region": "b", "other_alive": true, "forced_both": false, "note": "t_outage_start — moc 0 cua RTO clock"}
```

**Output từ loadgen:**
```
32 requests -> reports/drill-1-nodr.jsonl
```

**Kiểm tra drill-1-nodr.jsonl:**
- Tổng requests: 32
- Requests thành công (ok:true): 16 (lines 1-16)
- Requests thất bại (ok:false): 16 (lines 17-32)
- Dòng đầu tiên fail: line 17 (seq=16, ts=1791640848.711114, latency_ms=2024.6)
  - Thời gian fail: +0.384s từ t_outage (1791640848.711114 - 1791640848.3269491 = 0.384s)
- Error: ReadTimeout (2000+ ms timeout do netblock - quá hạn mạng)

**Kiểm tra chaos/chaos-events.jsonl:**
- Line 1: Kill event tại ts=1791640848.3269491 (ISO: 2026-10-10T14:00:48)
- Region bị kill: a
- Region khác còn sống: b (other_alive=true)

**Kết luận:**
Baseline drill chạy thành công. Region A bị SIGSTOP tại 2026-10-10T14:00:48, load gen phát hiện lỗi sau ~0.4 giây. 50% requests thất bại (16/32). Không có recovery — region B trống nên edge không thể failover tự động.

**Evidence path:**
- Outage: `chaos/chaos-events.jsonl:1`
- First failure: `reports/drill-1-nodr.jsonl:17`
- Last failure: `reports/drill-1-nodr.jsonl:32`

---

## S2. Đo RTO — kỳ vọng NO_RECOVERY

**Lệnh chạy:**
```bash
python3 tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300
```

**Output từ measure_rto.py (full JSON):**
```json
{
  "valid": true,
  "invalid_reasons": [],
  "warnings": [
    "khong co su kien UNHEALTHY cho region-a trong health log -> health checker khong chay, hoac chi log region phu",
    "khong co su kien 5_dns_cutover -> failover chay tay, khong tai lap duoc"
  ],
  "killed_region": "a",
  "recovered_by_region": null,
  "t_outage_iso": "2026-10-10T14:00:48",
  "chaos_mode": "netblock",
  "breakdown_seconds_from_t0": {
    "user_thay_loi_dau_tien": 0.4,
    "health_check_phat_hien": null,
    "dns_cutover": null,
    "request_thanh_cong_dau_tien": null
  },
  "health_check_config": {
    "interval_s": null,
    "threshold": null,
    "detect_floor_s": null
  },
  "rto_measured_s": null,
  "rto_target_s": 300.0,
  "rto_verdict": "NO_RECOVERY",
  "rpo_at_restore_s": null,
  "docs_lost": null,
  "requests_failed": 16,
  "evidence": {
    "loadgen": "reports/drill-1-nodr.jsonl",
    "chaos": "chaos/chaos-events.jsonl",
    "health": "reports/health-events.jsonl",
    "failover": "reports/failover-events.jsonl"
  }
}
```

**Kết quả chính:**
- `rto_verdict`: **NO_RECOVERY** ✓ (kỳ vọng)
- `rto_measured_s`: null (vì không có recovery)
- `requests_failed`: 16/32 = 50% fail rate
- `user_thay_loi_dau_tien`: 0.4 giây từ t_outage
- Warnings: Health checker và failover chưa implement (bình thường ở bước 2)

**Kết luận:**
Baseline không tự phục hồi — 16 requests thất bại và mãi mãi không recover. Đây là trạng thái mà Bước 3 sẽ khắc phục bằng health check + failover automation.

**Evidence path:** `reports/measure-drill-1.json`

---

## S3. Restore Region A (BẮTBUỘC --backend bare)

**Lệnh chạy:**
```bash
python3 chaos/kill_region.py restore --region a --backend bare
```

**Output:**
```
CHAOS {"ts": 1791640921.092799, "iso": "2026-10-10T14:02:01", "action": "restore", "region": "a", "method": "SIGCONT", "pid": 24752}
```

**Xác nhận restore thành công:**
```bash
curl localhost:8001/healthz
```
Kết quả: `{"region":"a","alive":true,"ts":1791640926.965405}`

```bash
curl localhost:8001/readyz
```
Kết quả: `{"region":"a","ready":true,"reasons":[],"pool_state":"full","vectors":{"count":200,"latest_doc_ts":1791522189.326974},"ts":1791640926.998223}`

```bash
curl localhost:8080/v1/infer
```
Kết quả: `{"edge_region":"a","upstream_status":200,"edge_latency_ms":24.8,"region":"a","answer":"[a] Ticket #0: khach hoi ve hoa don thang 1, so tien 0d","doc_id":"a-doc-0000",...}`

**Kết luận:**
Region A restored thành công:
- healthz: alive=true ✓
- readyz: ready=true ✓
- edge proxy: returning 200 responses with region=a ✓
- vector count: 200 documents ✓
- pool_state: full ✓

**Evidence path:** `chaos/chaos-events.jsonl:2` (restore event)

---

## S4. Test gate — baseline

**Lệnh chạy:**
```bash
pytest tests/test_rto_evidence.py::test_drill1_ton_tai_va_khong_phuc_hoi -v
```

**Output:**
```
============================= test session starts ==============================
platform darwin -- Python 3.11.15, pytest-8.3.4, pluggy-1.6.0 -- /Users/jaimes/.pyenv/versions/3.11.15/bin/python3.11
cachedir: .pytest_cache
rootdir: /Users/jaimes/Working/AI20K-IV/track-2/labs/Day23-Track2-Assignment
plugins: anyio-4.15.1
collecting ... collected 1 item

tests/test_rto_evidence.py::test_drill1_ton_tai_va_khong_phuc_hoi PASSED [100%]

============================== 1 passed in 0.06s =======================================
```

**Kết quả:** **PASSED** ✓

**Kết luận:** Test gate xác nhận baseline drill hợp lệ:
- Drill có sự kiện kill + failed requests
- RTO verdict: NO_RECOVERY (không tự recovery)
- Test assertions tất cả pass

---

## Tổng kết Bước 2 — RED TEAM

**Thời gian thực hiện:** 2026-10-10 21:00-21:02 (+07)

**Kết quả chính:**
| Chỉ số | Giá trị | Evidence |
|---|---|---|
| t_outage | 2026-10-10T14:00:48 | chaos/chaos-events.jsonl:1 |
| First failure | +0.4s | reports/drill-1-nodr.jsonl:17 |
| Total requests | 32 | reports/drill-1-nodr.jsonl |
| Requests failed | 16/32 (50%) | reports/drill-1-nodr.jsonl |
| RTO verdict | NO_RECOVERY | reports/measure-drill-1.json |
| Test result | PASSED | pytest output above |

**Mục tiêu hoàn tất:**
- [x] S1. Chạy baseline drill (kill region A, không DR) ✓
- [x] S2. Đo RTO — expect NO_RECOVERY ✓
- [x] S3. Restore Region A ✓
- [x] S4. Test gate passed ✓

**Bước tiếp theo:** Bước 3 sẽ implement health_checker, failover, runbook để tự động phục hồi trong drill-2 và đạt RTO ≤ 300s.
