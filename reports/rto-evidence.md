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
| Health-check detect floor | 15.0 (5s × 3) | `reports/health-events.jsonl:2` — `interval_s: 5.0, threshold: 3` | Giảm interval xuống ≤1s (cần cân nhắc flapping risk) |
| Detection + overhead thực tế | 20.13 | Từ kill (`chaos/chaos-events.jsonl:3`) đến failover start (`reports/failover-events.jsonl:1`) | Poll timeout + handoff lag |
| Snapshot restore | 0.03 | `reports/failover-events.jsonl:1→2` | Filesystem store đã tối ưu |
| GPU pool warm-up | 6.1 | `reports/failover-events.jsonl:4` — `waited_s:6.1` | Hạ WARMUP_SECONDS từ 6 xuống 3-4s |
| DNS/LB TTL cache | 2.4 | Từ DNS cutover (`reports/failover-events.jsonl:5`) đến first ok:true (`reports/drill-2-withdr.jsonl:39`) | Hạ EDGE_TTL_SECONDS từ 5 xuống 1s |
| **TỔNG** | **28.7s** | Phải ≈ RTO đo được (28.67s ≈ 28.7s ✓) | **Bottleneck: Detection (70%) + Warmup (21%)** |
