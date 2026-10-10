# Postmortem — DR Drill Lab 23

Blameless postmortem: câu hỏi là "hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-10T14:57:04 | Outage bắt đầu (kill region A) | `chaos/chaos-events.jsonl:3` |
| 2026-10-10T14:57:04 | User đầu tiên bị ảnh hưởng (ok:false) | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-10T14:57:23 | Health check phát hiện UNHEALTHY (region a) | `reports/health-events.jsonl:2` |
| 2026-10-10T14:57:30 | DNS cutover thực thi (failover step 5) | `reports/failover-events.jsonl:5` |
| 2026-10-10T14:57:32 | Resolved (first ok:true từ region b) | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- **RTO mục tiêu:** 300s (5 phút)
- **RTO đo được:** 28.7s
- **Gap:** 271.3s (mục tiêu vượt quá được)

- **RPO mục tiêu:** 300s (5 phút)
- **RPO đo được:** 8.01s (4 docs bị mất)
- **Gap:** 291.99s (mục tiêu vượt quá được)

**Bước tốn nhiều giây nhất:** Detection phase (20.13s = 70% RTO) — gồm:
  - 15.0s health check detect floor lý thuyết (interval=5s × threshold=3)
  - ~5s probe timeout + runbook confirm + handoff lag
  
**Bước thứ hai:** GPU pool warm-up (6.11s = 21% RTO)

Hai thành phần này chiếm 91% RTO tổng cộng ((20.13+6.11)/28.67 = 91%).

## 3. Root cause (5 whys) — Blameless

**Q: Tại sao RTO lại cao (28.7s) với health check detection floor?**

1. **Tại sao health checker phải poll mỗi 5s?** 
   - A: Cân bằng giữa tài nguyên poll (CPU, I/O) vs detection speed. Poll quá thường (1-2s) gây flapping từ network jitter; poll hiếm (10s+) trì hoãn phát hiện. 5s là điểm cân bằng hợp lý cho hầu hết SLA 300s.

2. **Tại sao threshold = 3 lần fail?**
   - A: Health check phải xác nhận 3 lần liên tiếp fail để chắc outage thật (không phải transient timeout). Mỗi lần fail có thể do network jitter, timeout client, hoặc server lag từ GC. Threshold=3 cung cấp bộ đệm.

3. **Tại sao detection floor (15s) là thành phần lớn nhất?**
   - A: Hệ thống đã thiết kế để detection phải chắc chắn (threshold=3). Nếu giảm threshold xuống 1 lần fail, risk flapping tăng (users sẽ thấy failover → failback loop nếu network có jitter). Nhân với interval=5s, soonest detection = 5s (1 poll fail) nhưng không safe. Nên 5×3=15s là soonest safe detection.

4. **Tại sao GPU pool warm-up mất 6.1s?**
   - A: serving/app.py:21 ghi WARMUP_SECONDS=6 (dòng 21). Khi pool_state chuyển từ "warm" → "full" (ở step 3_scale_pool), serving API ghi timestamp và chớp 6s warm-up timer (mô phỏng GPU pool warm-up) trước khi trả `ready=true`. Trong lab này, 6s mô phỏng thời gian chuẩn bị pool cho sản xuất.

5. **Tại sao DNS/LB TTL cache mất 2.4s từ cutover tới first ok:true?**
   - A: edge/proxy.py:16 ghi EDGE_TTL_SECONDS=5. Khi runbook ghi target vào edge/active_region ở step 5_dns_cutover (ts=1791644250.389384), edge proxy (resolve() function dòng 25-31) chỉ đọc lại file nếu cache hết hạn. 
   - **Bằng chứng từ log:** Từ cutover (failover-events.jsonl:5, ts=1791644250.389384) đến first ok:true (drill-2-withdr.jsonl:39, ts=1791644252.79165), khoảng cách = **2.402s ≈ 2.4s**. Trong khoảng này có request:
     - seq=37 (ts=1791644250.721): edge_region=a, status=503, error=ReadTimeout (cache vẫn giữ region cũ)
     - seq=38 (ts=1791644252.792): edge_region=b, status=200, ok:true (cache expire, request đến region mới)
   - **Giả định nguyên nhân:** Cache TTL=5s của edge/proxy.py (hàm resolve() chỉ đọc lại edge/active_region khi hết TTL) — log không phân tách được thành cache hit/miss/routing/execution nên không khẳng định thành phần nào chiếm bao lâu, nhưng bằng chứng rõ là cache còn giữ region cũ sau cutover.

## 4. Action items (có owner + deadline + ước lượng giảm RTO)

| # | Action | Owner | Deadline | Giảm RTO (s) | Ghi chú |
|---|---|---|---|---|---|
| 1 | Giảm health check interval từ 5s → 3s | SRE/Infra | 1 tuần | ~5-6s | Công thức: detection(3) ≈ 2.77 × 5 ≈ 13.8s; giảm ~5.6s; RTO mới ≈ 23s. Risk: flapping; threshold ≥3 |
| 2 | Tối ưu GPU pool warm-up: hạ WARMUP_SECONDS từ 6s → 3-4s | Serving/ML Ops | 2 tuần | ~2-3s | Giảm warm-up timeout; trade-off: cold startup risk vs. memory overhead |
| 3 | Hạ DNS/LB TTL cache từ 5s → 1s | Networking/Edge | 3 ngày | ~1-2s | Tăng cache miss rate nhưng không nghiêm trọng; trade-off: nhỏ |

**Tổng ước lượng giảm RTO nếu thực hiện cả 3 action:**
- Action 1 (detection): ~5-6s
- Action 2 (warm-up): ~2-3s
- Action 3 (DNS cache): ~1-2s
- **Cộng: ~8-11s** (RTO từ 28.7s → ~17-21s). Bottleneck vẫn là detection + warmup (~70% + 21%).

## 5. Ba câu hỏi bắt buộc trả lời

### Câu 1: `interval × threshold` = bao nhiêu? Nó chiếm % RTO?

**Trả lời:**
- `interval × threshold` = 5s × 3 = **15.0 giây** (detection floor lý thuyết)
- Detection thực tế (kill → failover start) = **20.13s** (includes probe/handoff overhead)
- % RTO: 20.13 / 28.7 = **70.2%** (làm tròn: **70%**)

**Giải thích:**
- 15.0s là soonest health checker có thể phát hiện outage lý thuyết (interval=5s × threshold=3 lần fail liên tiếp)
- Thực tế là 20.13s vì probe timeout (~2s mỗi lần) + runbook confirm + handoff giữa các thành phần làm chậm thêm ~5s
- Detection phase (20.13s) chiếm 70% RTO tổng cộng, cùng với GPU warmup (6.1s) làm thành bottleneck chính

### Câu 2: Nếu hạ interval xuống 1s, RTO giảm mấy giây? Trả giá gì?

**Trả lời (ƯỚC LƯỢNG, không phải số đo):**

- **Model chu kỳ health check (thống nhất):**
  - Mỗi chu kỳ = probe A (timeout 2s khi region down) + probe B (vài ms) + sleep(interval)
  - Khi region down: chu kỳ ≈ interval + 2s
  - Hiện tại (interval=5s): chu kỳ ≈ 5 + 2 = 7s; 3 lần fail liên tiếp ≈ 3 × 7 = 21s (lý thuyết)
  - Đo được: detection = 19.37s (rơi giữa chu kỳ); failover start = 20.13s (+ 0.76s handoff)
  - **Hệ số hiệu dụng:** 19.37 / 7 ≈ 2.77 chu kỳ → **công thức: detection(i) ≈ 2.77 × (i + 2)**

- **(a) Detection hiện tại (interval=5s):**
  - detection ≈ 2.77 × (5 + 2) ≈ 2.77 × 7 ≈ **19.4s**
  - Đo được: 19.37s ✓ khớp

- **(b) Detection nếu hạ interval=1s:**
  - detection ≈ 2.77 × (1 + 2) ≈ 2.77 × 3 ≈ **8.3s**
  - Giảm detection: 19.37 − 8.3 ≈ **11.1s**

- **(c) Mức giảm RTO:**
  - Failover start hiện tại: 20.13s (= detection 19.37s + 0.76s handoff)
  - Failover start mới ước lượng: 8.3s + 0.76s ≈ 9.1s
  - **Giảm mức RTO ~11s** (do detection là 70% của RTO)

- **(d) RTO mới ước lượng:**
  - RTO mới ≈ 28.7 − 11 ≈ **~18s** (so với 28.7s hiện tại)

- **Giá phải trả:**
  1. **Flapping risk:** Network jitter, GC pauses, transient timeouts sẽ trigger false UNHEALTHY → failover → failback liên tiếp
  2. **CPU/I/O overhead:** Poll 1s × 2 region = 2 HTTP GET/s per health checker; scale 1000 servers → 2000 req/s; bottleneck
  3. **False positives từ cloud provider:** Transient routing delays, temporary DNS timeouts

**Kết luận:** Giảm xuống 1s có thể lý thuyết nhưng rủi ro cao. 2-3s hợp lý hơn (nên giữ threshold ≥3 để chống flapping).

### Câu 3: Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost=4` có nghĩa gì?

**Trả lời:**
- **`docs_lost=4`** nghĩa là **4 documents/records bị mất** khi failover sang region phụ.
  
- **Tác động khách hàng:**
  - Với RPO=8.01s, snapshot của region phụ được lưu cách 8.01s so với lúc outage
  - Bất kỳ customer data ingested vào region chính trong 8.01s trước outage → không có trong snapshot → khách hàng thấy data loss
  - **Cụ thể:** 4 docs không sẵn có ở region phụ; nếu region chính mất vĩnh viễn (hardware failure, data corruption), 4 docs đó vĩnh viễn mất
  - **SLA impact:** Nếu SLA promise "≤ 5 min RPO", 8.01s vẫn OK; nếu promise "zero data loss", KPI failed
  
- **Mitigation:**
  - Increase replication frequency từ 30s → 10s (giảm RPO từ 8.01s → ~2-3s; **KHÔNG ảnh hưởng RTO đáng kể**)
  - Duy trì replicated write-ahead logs ở region phụ (WAL từ region chính; nếu region chính chết, WAL cho phép point-in-time recovery, docs_lost → 0)
  - Implement cross-region dual-write (active-active) để zero-loss, nhưng trade-off: conflict resolution complexity

- **Customer communication:**
  - "Tối đa 8 giây dữ liệu có thể bị mất trong failover từ region chính sang phụ"
  - "Để đạt RPO < 1 phút, bạn cần tier data theo priority (hot vs cold) và replicate nhanh hơn"
