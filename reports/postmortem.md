# Postmortem — DR Drill Lab 23

Blameless postmortem: câu hỏi là "hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-10T14:57:04 | Outage bắt đầu (kill region A) | `chaos/chaos-events.jsonl:3` |
| 2026-10-10T14:57:04 | User đầu tiên bị ảnh hưởng (ok:false) | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-10T14:57:23 | Health check phát hiện UNHEALTHY (region a) | `reports/health-events.jsonl:2` |
| 2026-10-10T14:57:30 | DNS cutover thực thi (failover step 5) | `reports/failover-events.jsonl:5` |
| 2026-10-10T14:57:33 | Resolved (first ok:true từ region b) | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- **RTO mục tiêu:** 300s (5 phút)
- **RTO đo được:** 28.7s
- **Gap:** 271.3s (mục tiêu vượt quá được)

- **RPO mục tiêu:** 300s (5 phút)
- **RPO đo được:** 8.01s (4 docs bị mất)
- **Gap:** 291.99s (mục tiêu vượt quá được)

**Bước tốn nhiều giây nhất:** Detection (20.13s = 70% RTO) — gồm:
  - 15.0s health check detect floor (interval=5s × threshold=3)
  - 5.13s overhead (probe timeout, runbook handoff, polling lag)
  
**Bước thứ hai:** GPU pool warm-up (6.1s = 21% RTO)

Hai thành phần này chiếm 91% RTO tổng cộng.

## 3. Root cause (5 whys) — Blameless

**Q: Tại sao RTO lại cao (28.7s) với health check detection floor?**

1. **Tại sao health checker phải poll mỗi 5s?** 
   - A: Cân bằng giữa tài nguyên poll (CPU, I/O) vs detection speed. Poll quá thường (1-2s) gây flapping từ network jitter; poll hiếm (10s+) trì hoãn phát hiện. 5s là điểm cân bằng hợp lý cho hầu hết SLA 300s.

2. **Tại sao threshold = 3 lần fail?**
   - A: Health check phải xác nhận 3 lần liên tiếp fail để chắc outage thật (không phải transient timeout). Mỗi lần fail có thể do network jitter, timeout client, hoặc server lag từ GC. Threshold=3 cung cấp bộ đệm.

3. **Tại sao detection floor (15s) là thành phần lớn nhất?**
   - A: Hệ thống đã thiết kế để detection phải chắc chắn (threshold=3). Nếu giảm threshold xuống 1 lần fail, risk flapping tăng (users sẽ thấy failover → failback loop nếu network có jitter). Nhân với interval=5s, soonest detection = 5s (1 poll fail) nhưng không safe. Nên 5×3=15s là soonest safe detection.

4. **Tại sao GPU pool warm-up mất 6.1s?**
   - A: Serving/app.py.py:21 ghi WARMUP=6s. Khi pool_state chuyển từ "warm" → "full" (ở step 3_scale_pool), serving API ghi timestamp và chớp 6s warm-up timer trước khi trả `ready=true`. 6s đó chứa GPU kernel compilation, model loading from disk, và memory allocation.

5. **Tại sao DNS/LB TTL cache chỉ mất 2.4s?**
   - A: EDGE_TTL_SECONDS=5s. Khi runbook ghi target vào edge/active_region ở step 5_dns_cutover, edge proxy vẫn cache upstream cũ (region a) cho tới TTL expired. Client không liền thấy routing change; phải chờ tối thiểu 1-2s cho cache re-resolve, cộng với TCP connection renegotiation.

## 4. Action items (có owner + deadline + ước lượng giảm RTO)

| # | Action | Owner | Deadline | Giảm RTO (s) | Ghi chú |
|---|---|---|---|---|---|
| 1 | Giảm health check interval từ 5s → 3s | SRE/Infra | 1 tuần | ~5-6s | Detect floor = 3×3=9s (ước lượng: tối thiểu -4s từ detection, +1s từ polling overhead); risk: flapping từ network jitter |
| 2 | Tối ưu GPU pool warm-up: hạ WARMUP_SECONDS từ 6s → 3-4s | Serving/ML Ops | 2 tuần | ~2-3s | Cần test GPU kernel caching, model prefetching; trade-off: cold startup vs. memory overhead |
| 3 | Hạ DNS/LB TTL cache từ 5s → 1s | Networking/Edge | 3 ngày | ~1-2s | Tăng cache miss rate nhưng không nghiêm trọng; trade-off: nhỏ |

**Tổng ước lượng giảm RTO nếu thực hiện cả 3:** ~8-11s (RTO từ 28.7s → ~17-21s). Bottleneck vẫn sẽ là detection + warmup.

## 5. Ba câu hỏi bắt buộc trả lời

### Câu 1: `interval × threshold` = bao nhiêu? Nó chiếm % RTO?

**Trả lời:**
- `interval × threshold` = 5s × 3 = **15.0 giây** (detection floor lý thuyết)
- Detection thực tế (kill → failover start) = **20.13s** (includes probe/handoff overhead)
- % RTO: 20.13 / 28.7 = **70.2%** (làm tròn: **70%**)

**Giải thích:**
- 15.0s là soonest health checker có thể phát hiện outage (5 lần poll mỗi 5s, lần thứ 3 fail thì flag UNHEALTHY)
- Thực tế là 20.13s vì runbook phải confirm, probe timeout, và handoff giữa các thành phần làm chậm thêm 5.13s
- 70% của RTO tổng cộng bị "bê" bởi detect floor, làm nó cùng với warmup trở thành bottleneck chính

### Câu 2: Nếu hạ interval xuống 1s, RTO giảm mấy giây? Trả giá gì?

**Trả lời:**
- **RTO giảm ước lượng:** ~5-6 giây
  - Detect floor mới = 1s × 3 = 3s (giảm từ 15s)
  - Overhead vẫn ~5s (probe timeout, runbook confirm) → detection thực tế ~ 8-9s
  - RTO mới ~ 22-23s (từ 28.7s)
  
- **Giá phải trả:**
  1. **Flapping risk:** Network jitter + GC pauses sẽ gây false positive UNHEALTHY → failover → failback, làm users bị phá vỡ session/cache
  2. **CPU/I/O overhead:** Poll mỗi 1s × 2 region = 2 HTTP GET/s; scale tới 1000 servers → 2000 req/s quay lại health checker endpoint; có thể gây bottleneck ở health checker server
  3. **False positives từ cloud provider:** Transient routing delays, temporary DNS timeouts sẽ trigger failover không cần thiết

**Kết luận:** Không khuyên hạ xuống 1s nếu không có monitoring + rollback automation chặt chẽ. Giảm xuống 2-3s có thể acceptable (detect ~6-9s) với proper tuning của timeout/threshold.

### Câu 3: Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost=4` có nghĩa gì?

**Trả lời:**
- **`docs_lost=4`** nghĩa là **4 documents/records bị mất** khi failover sang region phụ.
  
- **Tác động khách hàng:**
  - Với RPO=8.01s, snapshot của region phụ được lưu cách 8.01s so với lúc outage
  - Bất kỳ customer data ingested vào region chính trong 8.01s trước outage → không có trong snapshot → khách hàng thấy data loss
  - **Cụ thể:** 4 docs không sẵn có ở region phụ; nếu region chính mất vĩnh viễn (hardware failure, data corruption), 4 docs đó vĩnh viễn mất
  - **SLA impact:** Nếu SLA promise "≤ 5 min RPO", 8.01s vẫn OK; nếu promise "zero data loss", KPI failed
  
- **Mitigation:**
  - Increase replication frequency từ 30s → 10s (snapshot lạc hơn 3×, RTO giảm thêm ~3-5s)
  - Duy trì replicated write-ahead logs ở region phụ (WAL từ region chính; nếu region chính chết, WAL cho phép point-in-time recovery, docs_lost → 0)
  - Implement cross-region dual-write (active-active) để zero-loss, nhưng trade-off: conflict resolution complexity

- **Customer communication:**
  - "Tối đa 8 giây dữ liệu có thể bị mất trong failover từ region chính sang phụ"
  - "Để đạt RPO < 1 phút, bạn cần tier data theo priority (hot vs cold) và replicate nhanh hơn"
