# Self-report — Bước 0: Setup

- **Thời điểm chạy:** 2026-10-09 12:03:59 (+07); xác minh độc lập lúc 12:04:23
- **Môi trường:** Python 3.11.15 (pyenv, `/Users/jaimes/.pyenv/shims/python3`); `python3 -c "import httpx, fastapi, pytest"` → ok
- **Phạm vi:** chỉ setup — chưa viết code `dr/`, chưa chạy drill nào.

## Lệnh đã chạy và kết quả

| # | Lệnh | Kết quả |
|---|---|---|
| 1 | `make seed` | `seeded region-a: 200 docs`, `seeded region-b: 0 docs`, `edge/active_region` = `a` |
| 2 | `bash scripts/up_bare.sh` | region-a pid 24752 (:8001), region-b pid 24754 (:8002), edge pid 24756 (:8080), cả ba UP |
| 3 | `curl localhost:8001/healthz` | 200 `{"region":"a","alive":true}` |
| 4 | `curl localhost:8002/healthz` | 200 `{"region":"b","alive":true}` |
| 5 | `curl localhost:8080/edge/state` | 200 `{"active_region":"a","ttl_seconds":5.0}` |
| 6 | `curl localhost:8080/v1/infer` | 200 `edge_region:"a"`, answer `"[a] Ticket #0: ..."`, doc_id `a-doc-0000` |

## Trạng thái state sau seed (đã đối chiếu trực tiếp)

| Thành phần | Region A | Region B |
|---|---|---|
| `vectors.sqlite` | 49152 B, **200 rows** (bảng `docs`) | 16384 B, rỗng |
| `weights/model.bin` | 2097152 B (2 MiB) + `VERSION` | không có |
| `pool_state` | `full` | `warm` |
| `/readyz` | 200, `ready:true`, `count:200` | **503**, `ready:false` |

Region B `/readyz` reasons: `pool_state=warm`, `model_weights_missing`, `vector_db_empty(count=0)`.
`/v1/state` của B: `weights:false`, `count:0`.

## Quan sát dùng cho report nộp bài (Bước 1)

1. **Liveness ≠ readiness:** B trả `/healthz` 200 (`alive:true`) nhưng `/readyz` 503. Process sống không có nghĩa là region phục vụ được inference — đây là lý do health checker phải poll `/readyz`, không phải `/healthz`.
2. **Region B ban đầu trống có chủ đích:** thiếu cả 3 điều kiện (pool chưa `full`, chưa có weights, chưa có vector). Failover phải khôi phục đủ cả ba (restore snapshot + scale pool) trước khi cutover.
3. **Nếu flip `edge/active_region` sang `b` lúc này**, người dùng sẽ nhận 503 (`region_not_ready`) — cutover trước khi B ready làm hỏng cả hai region. (Chưa thử trực tiếp; sẽ thực hiện ở Bước 1.)

## Điều chỉnh so với CHECKLIST ban đầu

- CHECKLIST ghi B có `pool_state:cold`; thực tế là **`warm`**. Đã sửa.
- CHECKLIST ghi 503 chỉ do `vector_db_empty`; thực tế có 3 reasons. Đã sửa.

## Ghi chú

- Không gặp sự cố khi khởi động; 3 service đang chạy nền cho Bước 1.
- `git status` có `.python-version` (untracked) — file cấu hình pyenv, không thuộc bài nộp.
