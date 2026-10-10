# Runbook — Region Chính Down (Region A die → Failover sang Region B)

**Thời gian thực hiện:** ~15–20 phút (tùy detect delay + GPU warmup)  
**SLA:** RTO ≤ 5 phút, RPO ≤ 5 phút

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết code. Mỗi bước: lệnh copy-paste được + cách biết bước đó xong.

---

## 7 Bước Failover Tự Động

| # | Bước | Lệnh | Hoàn thành khi | Owner |
|---|---|---|---|---|
| 1 | **Xác nhận outage** | Probe region A: `curl -s localhost:8001/readyz \| jq .ready` | Trả về `false` 3 lần liên tiếp + chờ health_checker xác nhận UNHEALTHY (tối đa 60s) | On-call |
| 2 | **Mở incident + bấm giờ** | `python3 dr/runbook.py --primary a --target b --backend fs --auto` (bắt đầu automation) | Timestamp ghi vào `reports/runbook-run.jsonl`, step 2 `thong_bao_incident` | SRE / Incident Commander |
| 3 | **Restore state ở region phụ** | (Tự động trong runbook.py) | `cat reports/failover-events.jsonl` có dòng `step:2_restore_snapshot` | SRE / Automation |
| 4 | **Scale pool warm→full** | (Tự động trong runbook.py) | `curl -s localhost:8002/readyz \| jq .ready` trả `true` + `curl localhost:8080/edge/state \| jq .active_region` = `"b"` | SRE / Automation |
| 5 | **DNS/LB cutover** | (Tự động trong runbook.py) | `cat edge/active_region` = `b` | SRE / Automation |
| 6 | **Verify golden signals** | (Tự động trong runbook.py) | `tail -1 reports/runbook-run.jsonl \| jq '.p95_ms'` < 500 + error_rate < 5% | SRE / Monitoring |
| 7 | **Đo RTO + postmortem** | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict: PASS` (RTO ≤ 300s) | SRE / Postmortem Owner |

---

## Chi Tiết Bước 2: Chạy Automation Runbook

**Nếu muốn xác nhân trước khi failover (BÁN TỰ ĐỘNG):**
```bash
python3 dr/runbook.py --primary a --target b --backend fs
```
→ Sẽ hỏi confirm: "Proceed with failover? (y/N): " — gõ `y` để tiếp tục.

**Nếu muốn full-auto (chỉ dùng khi đã confirm outage):**
```bash
python3 dr/runbook.py --primary a --target b --backend fs --auto
```
→ Không hỏi, chạy tất cả 7 bước.

---

## Rollback Plan (Failover Ngược)

### Điều kiện Rollback (Region B → Region A)

Rollback (đổi lại DNS từ B sang A) phải **kiểm tra kỹ** trước:

1. **Region A đã sống lại?** 
   ```bash
   curl -s localhost:8001/readyz | jq .ready
   ```
   Phải trả `true`.

2. **Region A có dữ liệu hay bị mất vĩnh viễn?**
   - Nếu outage do hardware fail cần thay → bắt buộc phục hồi từ snapshot trước rollback
   - Nếu outage do mạng tạm → A vẫn giữ dữ liệu, safe rollback

3. **Region B đang phục vụ ổn định?**
   - Nếu B error rate > 10% → KHÔNG rollback, điều tra B
   - Nếu B ổn → safe rollback

### Quyết Định Rollback

- **Ai quyết định:** Incident Commander (senior SRE, engineering lead)
  - KHÔNG phải automated (§4 Anti-Patterns: full-auto flap gây outage kéo dài)
  - PHẢI là quyết định con người sau khi xem log

- **Cách rollback:**
  ```bash
  # Manual: đổi DNS
  echo a > edge/active_region
  
  # Hoặc dùng automation (nếu viết thêm):
  python3 dr/runbook.py --primary b --target a --backend fs --auto
  ```

- **Tiêu chí commit rollback:**
  - Region A sống + ổn định (error rate < 1%)
  - Region B không có sự cố tiếp diễn
  - RTO rollback ≤ 30s (vì A đã có dữ liệu sẵn)

---

## Troubleshooting

| Vấn đề | Nguyên nhân | Cách fix |
|---|---|---|
| Step 2: "snapshot không tìm được" | `replicate.py` chưa chạy trước failover, hoặc chưa put bao giờ | Khởi động: `python3 state/replicate.py --every 30 --duration 180 --backend fs` trước tấn công |
| Step 4: `/readyz` mãi không trả `true` | GPU pool warmup quá lâu, hoặc model weight corrupt | Kiểm tra `run/region-b.log`, restart `bash scripts/up_bare.sh` |
| Step 6: Error rate cao (> 10%) | Region B overload, hoặc vector DB có issue | Rollback sang A, debug B, retry sau |
| Cutover đã thực hiện nhưng user vẫn thấy lỗi | DNS cache (EDGE_TTL_SECONDS = 5s), hoặc client cache | Đợi 10s, retry request |

---

## Recovery Checkpoint

Sau khi failover xong, kiểm tra:

```bash
# 1. Verify active_region
cat edge/active_region              # Phải là 'b'

# 2. Verify readyz
curl localhost:8002/readyz | jq .ready       # Phải là true

# 3. Verify vector count
curl localhost:8002/v1/state | jq .count      # > 0

# 4. Verify single request works
curl localhost:8080/v1/infer -X POST -H "Content-Type: application/json" \
  -d '{"text":"test","region":"b"}' | jq .response

# 5. Measure RTO
python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300
# Cần: valid=true, rto_verdict=PASS (RTO ≤ 300s)
```

---

## Postmortem Questions (Sau failover thành công)

1. RTO thực tế so target (5 phút): Vượt hay trong?
2. RPO: Mất mấy document? Có SLA complaint không?
3. Detection delay: Health check mất bao lâu phát hiện?
4. Bottleneck: Bước nào tốn thời gian nhất? (restore? warmup? TTL cache?)
5. Có thể giảm RTO không? (ví dụ: giảm health check interval, pre-warm region B, ...)
