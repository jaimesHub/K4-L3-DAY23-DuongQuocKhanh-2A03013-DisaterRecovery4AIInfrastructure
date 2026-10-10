# RTO/RPO Evidence — Lab 23

Mỗi con số dưới đây đều được truy vết từ một dòng log thật (`đường/dẫn.jsonl:số_dòng`).

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | 2026-10-10T14:00:48 | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.4s | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/drill-1-nodr.jsonl` |
| RTO | NO_RECOVERY | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.2s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +19.4s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +20.2s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +26.3s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | +26.3s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | +28.7s | dòng `ok:true` đầu từ region b | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 28.7s | 300s (5 phút) | ✓ PASS |
| RPO — Vector DB | 8.01s / 4 docs | 300s (5 phút) | ✓ PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Detection (kill → failover start) | 20.13 | Từ `chaos/chaos-events.jsonl:3` đến `reports/failover-events.jsonl:1` | Giảm interval + probe timeout tuning |
| Snapshot restore | 0.03 | `reports/failover-events.jsonl:1→2` | Filesystem store đã tối ưu |
| GPU pool warm-up | 6.11 | `reports/failover-events.jsonl:4` — `waited_s:6.1` | Hạ WARMUP_SECONDS từ 6 xuống 3-4s |
| DNS/LB TTL cache | 2.40 | Từ `reports/failover-events.jsonl:5` đến `reports/drill-2-withdr.jsonl:39` | Hạ EDGE_TTL_SECONDS từ 5 xuống 1s |
| **TỔNG** | **28.67s ≈ 28.7s** | ✓ Khớp với RTO đo được | **Bottleneck: Detection (70%) + Warmup (21%)** |

**Ghi chú về detect floor:**
- Health check model: mỗi chu kỳ = interval + probe timeout (khi region down) ≈ 5 + 2 = 7s
- 3 lần fail liên tiếp (threshold=3) lý thuyết ≈ 21s, nhưng rơi giữa chu kỳ → 19.37s đo được
- Công thức: detection(i) ≈ 2.77 × (i + 2) với i = interval (giây)
- 15.0s (5×3) là "detect floor cơ bản" nhưng **không dùng** trong tổng RTO; thay vào đó dùng detection thực tế 19.37s (+ 0.76s handoff) ⇒ failover start 20.13s
- **Cộng thành phần:** 20.13 (detection+handoff) + 0.03 (snapshot) + 6.11 (warmup) + 2.40 (DNS cache) = 28.67s ✓
