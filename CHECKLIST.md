# Lab 23 — Region Failover DR/HA — CHECKLIST

**Lab thời gian:** 2 giờ | **Cách chạy:** 2 region (FastAPI 8001/8002), vector DB (SQLite), tấn công bằng kill_region.py

Danh sách công việc để hoàn thành bài lab. Mỗi bước ghi rõ: file cần sửa, lệnh chạy, pytest test, điểm số.

---

## BƯỚC 0 — SETUP (10 phút) — 0 điểm (điều kiện tiên quyết) — ✅ HOÀN THÀNH 2026-10-09 12:04 (+07)

> Evidence: [self-reports/step0-setup.md](self-reports/step0-setup.md)

- [x] **Cài đặt dependencies**
  - Lệnh: `pip install -r requirements.txt`
  - Kiểm tra: `python -c "import httpx; import fastapi; import pytest"`

- [x] **Seed state — Region A có 200 docs, Region B trống**
  - Lệnh: `make seed`
  - Kiểm tra: `curl localhost:8001/healthz` trả về `alive:true`; `curl localhost:8002/healthz` trả về `alive:true`
  - Ghi chú: File tạo ra `state/region-a/vectors.sqlite`, `state/region-a/weights/model.bin`, `edge/active_region` = `a`

- [x] **Khởi động 2 region + edge proxy (bare mode)**
  - Lệnh: `bash scripts/up_bare.sh`
  - Kiểm tra 3 healthz endpoint:
    ```bash
    curl localhost:8001/healthz   # Region A
    curl localhost:8002/healthz   # Region B
    curl localhost:8080/edge/state  # Edge proxy
    ```
  - Cả ba trả về HTTP 200 ✓ (đã xác minh)

- [x] **Test request qua edge proxy**
  - Lệnh: `curl localhost:8080/v1/infer`
  - Kỳ vọng: Trả về `edge_region:a` + câu trả lời bắt đầu với `"[a] ..."`

---

## BƯỚC 1 — BASELINE (15 phút) — 0 điểm (học tập) — ✅ HOÀN THÀNH 2026-10-09 12:46 (+07)

> Evidence: [self-reports/step1-baseline.md](self-reports/step1-baseline.md)

**TRƯỚC VIẾT CODE — đọc và trả lời 3 câu hỏi (ghi vào reports/postmortem.md sau):**

- [x] **Câu hỏi 1:** Nếu Region A chết ngay bây giờ, component nào sẽ phát hiện — và bằng cách nào?
  - Đáp án bạn phải nêu: `/readyz` endpoint (không phải `/healthz`)
  - Lý do: `/healthz` chỉ kiểm tra process còn sống; `/readyz` kiểm tra pool_state + vector count + model weights

- [x] **Câu hỏi 2:** Region B hiện tại có dữ liệu hay model weights không?
  - Lệnh kiểm tra: `curl localhost:8002/v1/state`
  - Kỳ vọng (đã đo ở Bước 0): `count:0`, `weights:false`, `pool_state:warm` (không phải `cold`)

- [x] **Câu hỏi 3:** Nếu flip `edge/active_region` thành `b` ngay bây giờ, users sẽ nhận gì?
  - Lệnh test: `echo b > edge/active_region && curl localhost:8080/v1/infer`
  - Kỳ vọng: HTTP 503; `/readyz` của B liệt kê `reasons: ["pool_state=warm","model_weights_missing","vector_db_empty(count=0)"]`
  - **RESTORE:** `echo a > edge/active_region`

- [x] **Đọc kỹ 3 file (đừng bỏ qua):**
  - `serving/app.py` dòng 62-80: `/readyz` logic (pool_state + warmup + weights + vector count)
  - `edge/proxy.py` dòng 25-31: DNS/LB caching logic (resolve() function), EDGE_TTL_SECONDS (line 16)
  - `state/snapshot.py` dòng 47-64: RPO computation (docs_lost + rpo_seconds)

---

## BƯỚC 2 — RED TEAM (25 phút) — 15 điểm (test_drill1_ton_tai_va_khong_phuc_hoi) — ✅ HOÀN THÀNH 2026-10-10 21:03 (+07)

> Evidence: [self-reports/step2-red-team.md](self-reports/step2-red-team.md)

**Chứng minh baseline không tự phục hồi:**

- [x] **Chạy baseline drill (tấn công Region A, không có DR):**
  ```bash
  python3 loadgen/traffic.py --duration 40 --rps 2 --out reports/drill-1-nodr.jsonl &
  sleep 8
  python3 chaos/kill_region.py --region a --mode netblock --mock
  wait
  ```
  - Ghi chú: `netblock --mock` = SIGSTOP (process treo, không kill); đợi load generator kết thúc
  - Output: `reports/drill-1-nodr.jsonl` (JSONL log mỗi request)

- [x] **Đo RTO — mong đợi NO_RECOVERY:**
  ```bash
  python3 tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300
  ```
  - Kỳ vọng: `rto_verdict: NO_RECOVERY`, `requests_failed > 0`
  - Output file: `reports/measure-drill-1.json`

- [x] **Restore Region A (BẮTBUỘC `--backend bare`):**
  ```bash
  python3 chaos/kill_region.py restore --region a --backend bare
  ```
  - Kiểm tra: `curl localhost:8001/healthz` trả về 200

- [x] **Test gate — baseline:**
  ```bash
  pytest tests/test_rto_evidence.py::test_drill1_ton_tai_va_khong_phuc_hoi -v
  ```
  - Điểm: **15 điểm** ✓

---

## BƯỚC 3 — CONTAINMENT — ✅ HOÀN THÀNH 2026-10-10 21:42 (+07)

> Evidence: [self-reports/step3-containment.md](self-reports/step3-containment.md)

**Điểm:** 65 điểm tổng (3a=15 + 3b=20 + 3c=0 + 3d=0) — 4 sub-step, mỗi cái commit riêng

### 3a. health_checker.py (12 phút) — 15 điểm

**File cần sửa:** `dr/health_checker.py`

**Yêu cầu:** Poll `/readyz` trên CẢ HAI region mỗi `interval` giây. Chỉ flip trạng thái sau `threshold` lần fail **LIÊN TIẾP**. Ghi JSONL mỗi lần đổi trạng thái.

**Implement:**

- [x] **Hàm `probe(region: str, timeout: float) -> tuple[bool, str]:`**
  - HTTP GET `http://127.0.0.1:800[1|2]/readyz` (region a→8001, region b→8002)
  - Timeout **PHẢI có** (để chặn netblock treo mãi)
  - Return: `(ready=bool, reason=str)` — nếu HTTP 200 và `ready=true` → ready, ngược lại không ready

- [x] **Hàm `run(interval, timeout, threshold, duration, out_path):`**
  - Vòng lặp: mỗi `interval` giây, gọi `probe()` cho region a và b
  - Đếm consecutive failures cho mỗi region
  - Khi failures ≥ `threshold` → emit `state_change` event (JSON line)
  - Reset counter khi probe thành công
  - **BẮT BUỘC ghi dòng JSONL khi state thay đổi**, có các field:
    - `ts` (Unix timestamp)
    - `region` (a|b)
    - `to` (HEALTHY|UNHEALTHY)
    - `reason` (chuỗi giải thích)
    - `interval_s` (giá trị interval dùng trong run này)
    - `threshold` (giá trị threshold dùng)
    - `consecutive_fails` (số lần fail liên tiếp)
  - Chạy lúc: `python dr/health_checker.py --interval 5 --threshold 3 --duration 300 --out reports/health-events.jsonl`

- [x] **Test — anti-flap (không flip dùng 1 lần fail):**
  ```bash
  pytest tests/test_failover.py::test_health_checker_can_threshold_lien_tiep -v
  ```
  - Kỳ vọng: Đầu tiên `to:UNHEALTHY`, `consecutive_fails >= 3`
  - **Điểm: 15 điểm** ✓

- [x] **Ý nghĩa RTO:** `interval × threshold` = detection floor (bạn sẽ ghi lại ở Step 4)
  - Ví dụ: 5s × 3 = 15s sớm nhất phát hiện outage

---

### 3b. failover.py (15 phút) — 20 điểm

**File cần sửa:** `dr/failover.py`

**Yêu cầu:** 5 bước **đúng thứ tự**, mỗi bước ghi 1 dòng JSONL vào `reports/failover-events.jsonl`.

**Implement:**

- [x] **Hàm `emit(**kw) -> None:`**
  - Append 1 dòng JSON (+ `ts` = Unix timestamp, `iso` = ISO8601) vào `reports/failover-events.jsonl`
  - Print ra stdout
  - Ghi chú: Không overwrite file, phải append

- [x] **Hàm `failover(target: str, backend: str, wait: float) -> dict:`**
  - 5 bước, **ĐÚNG THỨ TỰ NÀY**:
  
  **1️⃣ step 1_verify_target:**
    - HTTP GET `/v1/state` của target region (8001 hoặc 8002)
    - Log: `step:1_verify_target, region:target, pool_state:..., vector_count:..., weights:true|false`
    - Emit và in stdout
  
  **2️⃣ step 2_restore_snapshot:**
    - Gọi `state.snapshot.get(target, backend)` — restore từ snapshot
    - Lấy metadata: `rpo_seconds`, `docs_lost`, `embed_model_version` từ return value
    - Log: `step:2_restore_snapshot, rpo_seconds:X, docs_lost:Y, embed_model_version:Z`
    - **BẮTBUỘC ghi `rpo_seconds` và `docs_lost`** (tools/measure_rto.py cần đọc)
    - Emit và in stdout
  
  **3️⃣ step 3_scale_pool:**
    - Ghi `full` vào file `state/region-<target>/pool_state` (để /readyz thấy pool sẵn sàng)
    - Log: `step:3_scale_pool, region:target, to:full`
    - Emit
  
  **4️⃣ step 4_wait_ready:**
    - Poll HTTP GET `/readyz` của target với timeout
    - Lặp cho đến status 200 và `ready=true`, hoặc tới `wait` timeout
    - Log: `step:4_wait_ready, region:target, waited_s:X, status:200`
    - **NẾUTimeOut → ABORT** (không đi tới bước 5), return `{ok:false, reason:"target_timeout"}`
    - Emit
  
  **5️⃣ step 5_dns_cutover:**
    - Ghi target vào `edge/active_region`
    - Log: `step:5_dns_cutover, active_region:target`
    - Emit
  
  - Return `{ok:true, ...}` nếu tất cả 5 bước thành công; `{ok:false, reason:...}` nếu abort

- [x] **Test — không cutover nếu target chưa ready:**
  ```bash
  pytest tests/test_failover.py::test_failover_khong_cutover_khi_target_chua_ready -v
  ```
  - Kỳ vọng: Nếu target không bao giờ ready, không có `5_dns_cutover` log, `edge/active_region` không đổi
  - **Điểm: 20 điểm** ✓

---

### 3c. runbook.py (13 phút) — (không có điểm riêng, nhưng bắt buộc cho Step 4)

**File cần sửa:** `dr/runbook.py`

**Yêu cầu:** 7 bước automation, bán tự động (mặc định hỏi confirm; `--auto` cho CI).

**Implement:**

- [x] **Hàm `step(n, name, **kw) -> None:`**
  - Append 1 dòng JSON vào `reports/runbook-run.jsonl`
  - Dòng phải có: `ts` (Unix), `iso` (ISO8601), `step` (số/tên), `name` (tên), + các kw
  - Print ra stdout
  - Ghi chú: Đây là timeline của postmortem

- [x] **Hàm `confirm(auto: bool, msg: str) -> bool:`**
  - Nếu `auto=True` → return `True` (không hỏi, tự động chạy)
  - Nếu `auto=False` → hỏi `msg + " (y/N): "` → return True nếu user gõ 'y', False ngược lại

- [x] **Hàm `run(primary, target, backend, auto) -> dict:`**
  - **7 bước** (theo §4 Runbook):
  
  **1️⃣ xac_nhan_outage:**
    - Probe primary region (a hoặc b) 2-3 lần, phải fail tất cả
    - Log step với ts, name="xac_nhan_outage", alive=false
    - Call step() để ghi
  
  **2️⃣ thong_bao_incident:**
    - Log step với ts (MỘT LẦN DUY NHẤT đây là khoảnh khắc operator biết), name="thong_bao_incident"
    - **KHÔNG ghi lại t_outage từ chaos** — ghi ts của lúc runbook phát hiện
    - Ghi cả `t_outage_from_chaos` để postmortem tính detection delay
    - Call confirm() để hỏi proceed (trừ khi `--auto`)
  
  **3️⃣ scale_gpu_pool:**
    - Gọi `failover.failover(target, backend, wait=60)` **MỘT LẦN DUY NHẤT**
    - failover() tự ghi 5 dòng vào `reports/failover-events.jsonl`
    - Log step với result từ failover (ok, reason nếu failed)
    - Nếu failover failed → abort, return `{ok:false}`
  
  **4️⃣ verify_state_replica:**
    - KHÔNG gọi lại failover — chỉ ĐỌC kết quả từ dict failover trả về
    - Kiểm tra vector_count, weights_exist từ `state/region-<target>/`
    - Log step với replica_vector_count, weights_restored=true|false
  
  **5️⃣ dns_cutover:**
    - Cũng đọc từ kết quả failover (bước 5 đã cutover)
    - Kiểm tra `edge/active_region` = target
    - Log step với active_region=target, cutover_ok=true|false
  
  **6️⃣ verify_golden_signals:**
    - Gửi 10 request thật vào `localhost:8080/v1/infer` (qua edge proxy)
    - Ghi latency (p95), error_rate từ 10 request
    - Log step với requests_sent=10, errors=X, p95_ms=Y
  
  **7️⃣ post_incident:**
    - Tính elapsed_s từ step 2 (thong_bao_incident) đến giờ
    - Log step với elapsed_s, recommend_next="python tools/measure_rto.py ..."
    - Print guide để user đo RTO
  
  - Return dict: `{ok:true|false, ...}`
  - File log: `reports/runbook-run.jsonl`

- **Chạy dry-run (không cần test, KHÔNG tick — để dành cho Bước 4):**
  ```bash
  python dr/runbook.py --primary a --target b --backend fs --auto
  ```
  - Output: `reports/runbook-run.jsonl` + `reports/failover-events.jsonl` (từ failover step)
  - **GHI CHÚ:** Dry-run này sẽ thay đổi state/region-b + edge/active_region. Bước 4 PHẢI chạy với 3 service + ingest/replicate chạy nền. Nên để dành dry-run cho Bước 4.

---

### 3d. reports/runbook.md (10 phút) — (không có điểm riêng, nhưng yêu cầu chấm manual)

**File cần điền:** `reports/runbook.md`

**Yêu cầu:** 1 trang, 7 bước, **copy-pasteable**, owner, rollback condition.

**Template yêu cầu điền:**

- [x] **Bước 1 — Xác nhận outage**
  - Lệnh: `curl -s localhost:8001/readyz | jq .ready` (phải fail 3 lần liên tiếp)
  - Biết xong khi: Trả `false` 3 lần hoặc timeout
  - Owner: On-call

- [x] **Bước 2 — Mở incident + bấm giờ**
  - Lệnh: `python3 dr/runbook.py --primary a --target b --backend fs --auto` (hoặc bỏ --auto để xác nhân)
  - Biết xong khi: ts ghi vào `reports/runbook-run.jsonl`, step 2 `thong_bao_incident`
  - Owner: Incident Commander / SRE

- [x] **Bước 3 — Restore state**
  - Lệnh: (Tự động trong runbook.py, verify: `cat reports/failover-events.jsonl | jq '.[] | select(.step=="2_restore_snapshot")'`)
  - Biết xong khi: Dòng `2_restore_snapshot` có `rpo_seconds` + `docs_lost`
  - Owner: Automation / SRE

- [x] **Bước 4 — Scale pool warm → full**
  - Lệnh: (Tự động trong runbook.py, verify: `curl -s localhost:8002/readyz | jq .ready`)
  - Biết xong khi: Trả `true` + `cat edge/active_region` = `b`
  - Owner: Automation / SRE

- [x] **Bước 5 — DNS/LB cutover**
  - Lệnh: (Tự động trong runbook.py, verify: `cat edge/active_region`)
  - Biết xong khi: `active_region` = `b` + `curl localhost:8080/edge/state | jq .active_region`
  - Owner: Automation / SRE

- [x] **Bước 6 — Verify golden signals**
  - Lệnh: (Tự động trong runbook.py, verify: `tail -1 reports/runbook-run.jsonl | jq '{p95_ms, error_rate}'`)
  - Biết xong khi: p95_ms < 500ms + error_rate < 5%
  - Owner: Monitoring / SRE

- [x] **Bước 7 — Measure RTO + postmortem**
  - Lệnh: `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`
  - Biết xong khi: `rto_verdict = PASS` (RTO ≤ 300s)
  - Owner: Postmortem Owner / SRE

- [x] **Rollback plan (BẮTBUỘC):**
  - Câu hỏi: Điều kiện nào thì trả traffic về Region A?
  - Đáp: Region A sống + ổn định (error < 1%) + Region B không sự cố. RTO rollback ≤ 30s (A có dữ liệu sẵn)
  - Ai quyết định: Incident Commander (con người, KHÔNG auto). Full-auto flap gây outage kéo dài.
  - Ghi chú: Verify A sống trước rollback: `curl -s localhost:8001/readyz | jq .ready` → true

---

## BƯỚC 4 — PROVE & COLLECT EVIDENCE — ✅ HOÀN THÀNH 2026-10-10 21:59 (+07)

> Evidence: [self-reports/step4-prove.md](self-reports/step4-prove.md)

**⚠️ BƯỚC QUAN TRỌNG: Phải run ingest + replicate TRƯỚC tấn công, nếu không failover.py sẽ die ở step 2_restore_snapshot.**

- [x] **Khởi động ingest + replication (chạy nền):**
  ```bash
  python3 state/ingest.py --region a --rate 0.5 --duration 150 &
  python3 state/replicate.py --every 30 --duration 150 --backend fs &
  sleep 5   # đợi cycle replication đầu tiên xong
  ```
  - Output: `reports/replication.jsonl` (log mỗi cycle replicate)
  - Ghi chú: `replicate.py` cần ≥ 1 lần ghi snapshot thành công, nếu không `failover.py` sẽ không có snapshot để restore
  - ✓ Đã chạy, snapshot tạo thành công

- [x] **Chạy full drill với DR:**
  ```bash
  python3 loadgen/traffic.py --duration 100 --rps 2 --out reports/drill-2-withdr.jsonl &
  python3 dr/health_checker.py --interval 5 --threshold 3 --duration 100 --out reports/health-events.jsonl &
  sleep 12
  python3 chaos/kill_region.py --region a --mode netblock --mock
  python3 dr/runbook.py --primary a --target b --backend fs --auto
  wait
  ```
  - Output files:
    - `reports/drill-2-withdr.jsonl` — 156 requests, every request timestamp + ok/fail ✓
    - `reports/health-events.jsonl` — health check state changes ✓
    - `reports/failover-events.jsonl` — 5 failover steps ✓
    - `reports/runbook-run.jsonl` — 7 runbook steps (file .gitignored)
    - `chaos/chaos-events.jsonl` — kill event timestamp ✓

- [x] **Đo RTO từ log (lần đầu xem kết quả):**
  ```bash
  python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300
  ```
  - Output: `reports/measure-drill-2.json` (kết quả tính toán RTO/RPO) — file .gitignored
  - ✓ rto_verdict: PASS, rto_measured_s: 28.7, valid: true, warnings: []

- [x] **Test — RTO ≤ 300s:**
  ```bash
  pytest tests/test_rto_evidence.py::test_drill2_hop_le -v
  ```
  - ✓ PASSED: valid=true, warnings=[] (empty list)
  - **Điểm: 20 điểm** ✓

- [x] **Test — RTO đo từ timestamp thật:**
  ```bash
  pytest tests/test_rto_evidence.py::test_rto_do_duoc_bang_timestamp -v
  ```
  - ✓ PASSED: rto_measured_s=28.7, recovered_by_region=b != killed_region=a
  - **Điểm: (bao gồm trong 20 điểm trên)** ✓

- [x] **Test — RPO đo (seconds + documents):**
  ```bash
  pytest tests/test_failover.py::test_rpo_dem_dung_so_doc_bi_mat -v
  pytest tests/test_rto_evidence.py::test_rpo_duoc_do_chu_khong_uoc_luong -v
  ```
  - ✓ PASSED: rpo_at_restore_s=8.01, docs_lost=4
  - **Điểm: 10 điểm** ✓

---

## BƯỚC 5 — FILL REPORTS (20 phút) — 20 điểm — ✅ HOÀN THÀNH 2026-10-10 22:20 (+07)

> Evidence: [self-reports/step5-reports.md](self-reports/step5-reports.md)

### 5a. reports/rto-evidence.md — 10 điểm — ✅ HOÀN THÀNH

**File cần điền:** `reports/rto-evidence.md`

**Yêu cầu:** Mỗi con số PHẢI trỏ được về 1 dòng log thật (path:line). Không có evidence = trượt.

**Điền như sau:**

- [x] **Section "1. Drill 1 — không có DR":**
  | Chỉ số | Giá trị | Evidence |
  |---|---|---|
  | t_outage | ISO8601 timestamp từ chaos-events.jsonl:1 | `chaos/chaos-events.jsonl:1` |
  | Request fail đầu tiên | `+__s` từ first ok:false sau outage | `reports/drill-1-nodr.jsonl:17` |
  | Request thành công sau | không có | `reports/drill-1-nodr.jsonl` |
  | RTO verdict | NO_RECOVERY | `reports/measure-drill-1.json` |

- [x] **Section "2. Drill 2 — có DR" — 7 mốc:**
  - t_outage (0s): `chaos/chaos-events.jsonl:3`
  - User thấy lỗi: `reports/drill-2-withdr.jsonl:25` (+0.2s)
  - Health check phát hiện: `reports/health-events.jsonl:2` (+19.4s)
  - Snapshot restore xong: `reports/failover-events.jsonl:2` (+20.2s)
  - Region phụ ready: `reports/failover-events.jsonl:4` (+26.3s)
  - DNS cutover: `reports/failover-events.jsonl:5` (+26.3s)
  - **RTO đo được:** `reports/drill-2-withdr.jsonl:39` (+28.7s) ← **Đây là RTO cuối cùng**

- [x] **Section "3. RTO breakdown" — 4 thành phần:**
  | Thành phần | Giây | Ghi chú |
  |---|---|---|
  | Health-check detect floor | 15.0 (5s × 3) | `reports/health-events.jsonl:2` (interval_s=5, threshold=3) |
  | Detection + overhead | 20.13 | Từ kill (`chaos/chaos-events.jsonl:3`) đến failover start (`reports/failover-events.jsonl:1`) |
  | Snapshot restore | 0.03 | `reports/failover-events.jsonl:1→2` |
  | GPU pool warm-up | 6.1 | `reports/failover-events.jsonl:4` (waited_s=6.1) |
  | DNS/LB TTL cache | 2.4 | Từ `reports/failover-events.jsonl:5` đến `reports/drill-2-withdr.jsonl:39` |
  | **TỔNG** | **28.7s** | ≈ RTO đo được (28.67s ≈ 28.7s) ✓ |

- [x] **Test — Evidence trỏ đúng file/line:**
  ```bash
  pytest tests/test_rto_evidence.py::test_evidence_table_tro_vao_file_that -v
  ```
  - ✓ PASSED

- [x] **Test — Không còn placeholder:**
  ```bash
  pytest tests/test_rto_evidence.py::test_evidence_table_da_dien_that -v
  ```
  - ✓ PASSED

- [x] **Test — Con số khớp log (HARD-FAIL nếu sai > 1s):**
  ```bash
  pytest tests/test_rto_evidence.py::test_so_trong_bang_khop_voi_so_trong_log -v
  ```
  - ✓ PASSED

- [x] **Test — Health check config ghi lại:**
  ```bash
  pytest tests/test_rto_evidence.py::test_health_check_interval_duoc_ghi_lai -v
  ```
  - ✓ PASSED

---

### 5b. reports/postmortem.md — 10 điểm — ✅ HOÀN THÀNH

**File cần điền:** `reports/postmortem.md`

**Yêu cầu:** Blameless postmortem (câu hỏi là "hệ thống cho phép điều gì", không phải "ai làm sai").

**Điền theo template §4:**

- [x] **Section "1. Timeline" — 5 sự kiện:**
  | ISO time | Sự kiện | Evidence |
  |---|---|---|
  | 2026-10-10T14:57:04 | Outage bắt đầu (kill signal) | `chaos/chaos-events.jsonl:3` |
  | 2026-10-10T14:57:04 | User đầu tiên bị ảnh hưởng (ok:false) | `reports/drill-2-withdr.jsonl:25` |
  | 2026-10-10T14:57:23 | Health check phát hiện UNHEALTHY | `reports/health-events.jsonl:2` |
  | 2026-10-10T14:57:30 | DNS cutover (failover step 5) | `reports/failover-events.jsonl:5` |
  | 2026-10-10T14:57:33 | Resolved (first ok:true từ region b) | `reports/drill-2-withdr.jsonl:39` |

- [x] **Section "2. RTO/RPO vs Target — gap analysis":**
  - RTO mục tiêu: 300s | RTO đo được: 28.7s | Gap: 271.3s (vượt quá được)
  - RPO mục tiêu: 300s | RPO đo được: 8.01s / 4 docs | Gap: 291.99s (vượt quá được)
  - **Bước tốn nhiều giây nhất:** Detection (20.13s = 70% RTO) — gồm 15s floor (5s × 3) + 5.13s overhead

- [x] **Section "3. Root cause (5 whys)" — Blameless:**
  - Detection floor 15s: interval=5s × threshold=3 để chống flapping từ network jitter
  - Threshold=3: xác nhận 3 lần fail liên tiếp để chắc outage thật (không transient timeout)
  - GPU warm-up 6s: WARMUP_SECONDS hard-coded, bao gồm GPU kernel compile + model load từ disk
  - DNS TTL cache 2.4s: EDGE_TTL_SECONDS=5s, client không liền thấy routing change

- [x] **Section "4. Action items" — Owner + Deadline + Impact:**
  | # | Action | Owner | Deadline | Giảm RTO (s) |
  |---|---|---|---|---|
  | 1 | Giảm health check interval từ 5s → 3s | SRE/Infra | 1 tuần | 5-6s |
  | 2 | Tối ưu GPU warm-up: hạ WARMUP_SECONDS từ 6s → 3-4s | Serving/ML Ops | 2 tuần | 2-3s |
  | 3 | Hạ DNS TTL cache từ 5s → 1s | Networking/Edge | 3 ngày | 1-2s |

- [x] **Section "5. Ba câu hỏi bắt buộc trả lời":**
  1. `interval × threshold` = 5 × 3 = **15.0s** lý thuyết; **20.13s** thực tế; chiếm **70%** RTO
  2. Hạ interval xuống 1s → detect floor = 3s → RTO giảm ~5-6s (22-23s) nhưng trade-off: flapping risk từ network jitter, CPU/I/O overhead từ 2 poll/s × 2 region
  3. `docs_lost=4` → 4 records không sẵn có ở region phụ; nếu region chính mất vĩnh viễn → 4 docs vĩnh viễn mất. Mitigation: increase replication frequency → 10s (RPO tuân thủ < 1 min SLA) hoặc implement dual-write/WAL

- [x] **Test — Postmortem đủ yếu tố:**
  ```bash
  pytest tests/test_rto_evidence.py::test_postmortem_co_gap_analysis -v
  ```
  - ✓ PASSED: Có `rto`, `rpo`, `gap`, `action item`

---

## BƯỚC 6 — VERIFY & SUBMIT (5 phút) — 0 điểm (gate cuối)

- [ ] **Chạy toàn bộ pytest:**
  ```bash
  python3 -m pytest tests/ -v
  ```
  - Kỳ vọng: Tất cả test xanh (PASSED)
  - Nếu FAILED → debug theo error message (xem phần Hard-Fail Conditions dưới)

- [ ] **Kiểm tra không có hard-fail:**
  - ✅ `measure_rto.py` trả `valid:true` (test `test_drill2_hop_le`)
  - ✅ RTO/RPO trong rto-evidence.md khớp log (test `test_so_trong_bang_khop_voi_so_trong_log`)
  - ✅ Không có double outage (test `test_chaos_khong_giet_ca_hai_region`)
  - ✅ Cutover **SAU** health check phát hiện (tự check: t_cutover > t_detect)
  - ✅ Recovery phục vụ bởi region **KHÁC** region bị kill (test `test_rto_do_duoc_bang_timestamp`)

---

## HARD-FAIL CONDITIONS (Trượt nếu vi phạm)

| Điều kiện | Dấu hiệu | Cách fix |
|---|---|---|
| **1. Drill không hợp lệ** | `tools/measure_rto.py` trả `valid:false` | Xem `invalid_reasons` — thường do: (a) load gen + chaos không overlap, (b) no failed request, (c) double outage |
| **2. RTO không khớp log** | `test_so_trong_bang_khop_voi_so_trong_log` FAIL | Con số trong rto-evidence.md sai > 1s → bị tính "tự nghĩ ra". Re-run drill, copy đúng số từ measure-drill-2.json |
| **3. Cutover trước detect** | `t_cutover < t_detect` | Chứng tỏ runbook.py cắt DNS trước health checker phát hiện outage. Re-run drill với `--auto`. |
| **4. Double outage** | `forced_both` hoặc `other_alive=false` ở kill event | Vô tình kill cả 2 region (hoặc dùng `--i-really-want-both` — marks drill INVALID). Restore region A trước tấn công lại. |
| **5. Evidence không trỏ thực** | `test_evidence_table_tro_vao_file_that` FAIL | Dòng trong rto-evidence.md tham chiếu file/line không tồn tại. Check path + line number (1-indexed). |
| **6. Recovery không từ region phụ** | `test_rto_do_duoc_bang_timestamp` FAIL `recovered_by_region == killed_region` | Chứng tỏ region A tự phục hồi (mâu thuẫn SO3, phải failover sang B). Kiểm tra edge/active_region được set thành B. |
| **7. Snapshots không bao giờ được put** | failover.py die ở step 2_restore_snapshot | Forgot `python state/replicate.py ... &` trước tấn công. Must wait ≥ 1 replicate cycle before attack. |

---

## PITFALL QUAN TRỌNG (Đừng mắc phải)

| Pitfall | Lý do | Cách tránh |
|---|---|---|
| **1. Sửa file ngoài `dr/`** | Spec nói "do not modify anything outside dr/ to pass test" | Tất cả logic phải ở `dr/health_checker.py`, `dr/failover.py`, `dr/runbook.py`. Không sửa `serving/app.py`, `edge/proxy.py`, `state/snapshot.py` |
| **2. Đổi tay `edge/active_region`** | `measure_rto.py` sẽ phát hiện cutover thủ công (t_cutover < t_detect) → cảnh báo → test fail | Để runbook.py automation đổi (bước 5_dns_cutover của failover()) |
| **3. Health check flip quá nhanh (1 lần fail = UNHEALTHY)** | Test yêu cầu threshold ≥ 3 để chống flapping | Mỗi failed probe phải countdown threshold, chỉ flip khi counter = 0 |
| **4. Ghi RTO "by feel"** | pytest sẽ kiểm tra con số khớp log (sai > 1s = trượt) | Dùng `tools/measure_rto.py` để lấy RTO chính xác, copy từ `rto_measured_s` vào bảng |
| **5. Snapshot không có interval_s/threshold** | `tools/measure_rto.py` cần đọc từ health-events.jsonl để tính detection floor | health_checker.py PHẢI ghi `interval_s` và `threshold` ở mỗi dòng state_change |
| **6. Runbook gọi failover nhiều lần** | failover() sẽ ghi multiple dòng 5_dns_cutover → measure_rto.py nhầm t_cutover → sai RTO | Gọi failover **MỘT LẦN DUY NHẤT** ở step 3, chỉ ĐỌC kết quả sau đó |
| **7. Dùng Docker mode cho graded drill** | Timing không reproducible → RTO thay đổi mỗi run → pytest fail | Luôn dùng `--backend bare` + `--mode netblock --mock` + `--backend fs` |
| **8. Quên `--backend bare` khi restore** | `chaos/kill_region.py restore` mặc định dùng Docker (sẽ fail nếu không có daemon) | Luôn ghi `restore --region a --backend bare` |
| **9. RPO không có docs_lost** | Test `test_rpo_duoc_do_chu_khong_uoc_luong` yêu cầu cả rpo_seconds + docs_lost | failover.py bước 2 phải ghi `rpo_seconds:X` và `docs_lost:Y` vào JSON |
| **10. Postmortem chỉ nêu triệu chứng, không Root Cause** | Spec yêu cầu "nếu outage thật, bước nào runbook thất bại" | Phân tích từng thành phần RTO breakdown, giải thích TẠI SAO lại tốn thời gian đó |

---

## QUYẾT TOÁN ĐIỂM

| Công việc | Test / Điểm | Tối đa |
|---|---|---|
| **Bước 0** | (tiên quyết) | 0 |
| **Bước 1** | (học tập) | 0 |
| **Bước 2** | `test_drill1_ton_tai_va_khong_phuc_hoi` | **15** |
| **Bước 3a** | `test_health_checker_can_threshold_lien_tiep` | **15** |
| **Bước 3b** | `test_failover_khong_cutover_khi_target_chua_ready` | **20** |
| **Bước 3c** | (automation, không test riêng) | 0 |
| **Bước 3d** | (manual review) | 0 |
| **Bước 4** | `test_drill2_hop_le` + `test_rto_do_duoc_bang_timestamp` | **20** |
| **Bước 4** | `test_rpo_duoc_do_chu_khong_uoc_luong` | **10** |
| **Bước 5a** | `test_evidence_table_*` + `test_so_trong_bang_khop_voi_so_trong_log` + `test_health_check_interval_duoc_ghi_lai` | **10** |
| **Bước 5b** | `test_postmortem_co_gap_analysis` | **10** |
| **TỔNG CỘNG** | | **100** |

---

## CHEAT SHEET — LỆNH THƯỜNG DÙNG

```bash
# Setup & seed
make clean                          # Xoá state + log cũ
make seed                           # Seed region A (200 docs), region B (empty)
bash scripts/up_bare.sh             # Khởi động 2 region + edge
bash scripts/down_bare.sh           # Tắt tất cả

# Drill 1 (baseline, không DR)
make drill-baseline                 # Hoặc chạy tay:
python3 loadgen/traffic.py --duration 40 --rps 2 --out reports/drill-1-nodr.jsonl &
sleep 8; python3 chaos/kill_region.py --region a --mode netblock --mock; wait

# Drill 2 (với DR) — PHẢI run replicate trước
python3 state/ingest.py --region a --rate 0.5 --duration 150 &
python3 state/replicate.py --every 30 --duration 150 --backend fs &
sleep 5
make drill-dr                       # Hoặc manual tương tự

# Measure RTO
python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300

# Test
pytest tests/test_failover.py -v                      # Unit test (không cần server)
pytest tests/test_rto_evidence.py -v                  # Evidence gate (cần log từ drill)
python3 -m pytest tests/ -v                           # Tất cả

# Debug
curl localhost:8001/healthz         # Health region A
curl localhost:8002/readyz          # Readiness region B
curl localhost:8080/edge/state      # Edge state
tail -f run/region-a.log            # Log stream region A
cat chaos/chaos-events.jsonl        # Kill events
```

---

## TÀI LIỆU THAM KHẢO

| File | Mục đích | Cần đọc lúc |
|---|---|---|
| README.md | Tổng quan lab | Bước 0 |
| GUIDE.md | Walkthrough chi tiết + common pitfalls | Bước 0, khi stuck |
| RUBRIC.md | Grading criteria + hard-fail + scoring | Bước 5 (check scoring) |
| serving/app.py | /readyz logic (pool_state + warmup + vectors + weights) | Bước 1 (câu hỏi 1, 3) |
| edge/proxy.py | DNS/LB routing (EDGE_TTL_SECONDS caching) | Bước 1 (câu hỏi 3) |
| state/snapshot.py | RPO + restore logic | Bước 3b (failover step 2) |
| chaos/kill_region.py | Attack mechanics (SIGSTOP vs SIGKILL, --mock) | Bước 2 |
| loadgen/traffic.py | RTO measurement clock | Bước 4 |
| tools/measure_rto.py | RTO/RPO computation | Bước 4, 5a debug |
| tests/test_failover.py | Unit test expectations | Bước 3 code review |
| tests/test_rto_evidence.py | Evidence gate expectations | Bước 5 code review |

---

**Cuối cùng: Chạy `python3 -m pytest tests/ -v` — nếu tất cả xanh, bài lab XONG.**
