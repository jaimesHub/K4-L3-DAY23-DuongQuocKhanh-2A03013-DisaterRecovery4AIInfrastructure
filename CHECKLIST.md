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

## BƯỚC 1 — BASELINE (15 phút) — 0 điểm (học tập) — ✅ HOÀN THÀNH 2026-10-09 05:45 (+07)

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

## BƯỚC 2 — RED TEAM (25 phút) — 15 điểm (test_drill1_ton_tai_va_khong_phuc_hoi)

**Chứng minh baseline không tự phục hồi:**

- [ ] **Chạy baseline drill (tấn công Region A, không có DR):**
  ```bash
  python3 loadgen/traffic.py --duration 40 --rps 2 --out reports/drill-1-nodr.jsonl &
  sleep 8
  python3 chaos/kill_region.py --region a --mode netblock --mock
  wait
  ```
  - Ghi chú: `netblock --mock` = SIGSTOP (process treo, không kill); đợi load generator kết thúc
  - Output: `reports/drill-1-nodr.jsonl` (JSONL log mỗi request)

- [ ] **Đo RTO — mong đợi NO_RECOVERY:**
  ```bash
  python3 tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300
  ```
  - Kỳ vọng: `rto_verdict: NO_RECOVERY`, `requests_failed > 0`
  - Output file: `reports/measure-drill-1.json`

- [ ] **Restore Region A (BẮTBUỘC `--backend bare`):**
  ```bash
  python3 chaos/kill_region.py restore --region a --backend bare
  ```
  - Kiểm tra: `curl localhost:8001/healthz` trả về 200

- [ ] **Test gate — baseline:**
  ```bash
  pytest tests/test_rto_evidence.py::test_drill1_ton_tai_va_khong_phuc_hoi -v
  ```
  - Điểm: **15 điểm** ✓

---

## BƯỚC 3 — CONTAINMENT (50 phút) — 65 điểm tổng (3a + 3b + 3c + 3d)

### 3a. health_checker.py (12 phút) — 15 điểm

**File cần sửa:** `dr/health_checker.py`

**Yêu cầu:** Poll `/readyz` trên CẢ HAI region mỗi `interval` giây. Chỉ flip trạng thái sau `threshold` lần fail **LIÊN TIẾP**. Ghi JSONL mỗi lần đổi trạng thái.

**Implement:**

- [ ] **Hàm `probe(region: str, timeout: float) -> tuple[bool, str]:`**
  - HTTP GET `http://127.0.0.1:800[1|2]/readyz` (region a→8001, region b→8002)
  - Timeout **PHẢI có** (để chặn netblock treo mãi)
  - Return: `(ready=bool, reason=str)` — nếu HTTP 200 và `ready=true` → ready, ngược lại không ready

- [ ] **Hàm `run(interval, timeout, threshold, duration, out_path):`**
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

- [ ] **Test — anti-flap (không flip dùng 1 lần fail):**
  ```bash
  pytest tests/test_failover.py::test_health_checker_can_threshold_lien_tiep -v
  ```
  - Kỳ vọng: Đầu tiên `to:UNHEALTHY`, `consecutive_fails >= 3`
  - **Điểm: 15 điểm** ✓

- [ ] **Ý nghĩa RTO:** `interval × threshold` = detection floor (bạn sẽ ghi lại ở Step 4)
  - Ví dụ: 5s × 3 = 15s sớm nhất phát hiện outage

---

### 3b. failover.py (15 phút) — 20 điểm

**File cần sửa:** `dr/failover.py`

**Yêu cầu:** 5 bước **đúng thứ tự**, mỗi bước ghi 1 dòng JSONL vào `reports/failover-events.jsonl`.

**Implement:**

- [ ] **Hàm `emit(**kw) -> None:`**
  - Append 1 dòng JSON (+ `ts` = Unix timestamp, `iso` = ISO8601) vào `reports/failover-events.jsonl`
  - Print ra stdout
  - Ghi chú: Không overwrite file, phải append

- [ ] **Hàm `failover(target: str, backend: str, wait: float) -> dict:`**
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

- [ ] **Test — không cutover nếu target chưa ready:**
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

- [ ] **Hàm `step(n, name, **kw) -> None:`**
  - Append 1 dòng JSON vào `reports/runbook-run.jsonl`
  - Dòng phải có: `ts` (Unix), `iso` (ISO8601), `step` (số/tên), `name` (tên), + các kw
  - Print ra stdout
  - Ghi chú: Đây là timeline của postmortem

- [ ] **Hàm `confirm(auto: bool, msg: str) -> bool:`**
  - Nếu `auto=True` → return `True` (không hỏi, tự động chạy)
  - Nếu `auto=False` → hỏi `msg + " (y/N): "` → return True nếu user gõ 'y', False ngược lại

- [ ] **Hàm `run(primary, target, backend, auto) -> dict:`**
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

- [ ] **Chạy dry-run (không cần test):**
  ```bash
  python dr/runbook.py --primary a --target b --backend fs --auto
  ```
  - Output: `reports/runbook-run.jsonl` + `reports/failover-events.jsonl` (từ failover step)

---

### 3d. reports/runbook.md (10 phút) — (không có điểm riêng, nhưng yêu cầu chấm manual)

**File cần điền:** `reports/runbook.md`

**Yêu cầu:** 1 trang, 7 bước, **copy-pasteable**, owner, rollback condition.

**Template yêu cầu điền:**

- [ ] **Bước 1 — Xác nhận outage**
  - Lệnh: (có thể dùng lệnh probe từ health_checker, hoặc `chaos/kill_region.py status`)
  - Biết xong khi: (cách biết region chính chết)
  - Owner: (ai chịu trách nhiệm — e.g., on-call, infra, SRE)

- [ ] **Bước 2 — Mở incident + bấm giờ**
  - Lệnh: (để runbook.py ghi ts, hoặc alert tool nào đó)
  - Biết xong khi: (ts được ghi vào runbook-run.jsonl)
  - Owner:

- [ ] **Bước 3 — Restore state**
  - Lệnh: `python state/snapshot.py get --region b --backend fs`
  - Biết xong khi: (vector count được phục hồi)
  - Owner:

- [ ] **Bước 4 — Scale pool warm → full**
  - Lệnh: (ghi full vào state/region-b/pool_state, hoặc gọi failover automation)
  - Biết xong khi: (curl localhost:8002/readyz trả 200)
  - Owner:

- [ ] **Bước 5 — DNS/LB cutover**
  - Lệnh: `echo b > edge/active_region`
  - Biết xong khi: (curl localhost:8080/edge/state cho active_region=b)
  - Owner:

- [ ] **Bước 6 — Verify golden signals**
  - Lệnh: (10 request thật vào API, kiểm tra latency + error rate)
  - Biết xong khi: (p95 < 500ms, error < 1%)
  - Owner:

- [ ] **Bước 7 — Measure RTO + postmortem**
  - Lệnh: `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl`
  - Biết xong khi: (rto_verdict = PASS)
  - Owner:

- [ ] **Rollback plan (BẮTBUỘC):**
  - Câu hỏi: Điều kiện nào thì trả traffic về Region A?
  - Đáp: (e.g., nếu region B error rate > 5%, bất kỳ ai có quyền incident commander có thể quyết định)
  - Ai quyết định: (role/person)
  - Ghi chú: §4 cảnh báo full-auto failover không có circuit breaker sẽ flap giữa 2 region

---

## BƯỚC 4 — PROVE & COLLECT EVIDENCE (20 phút) — 20 điểm

**⚠️ BƯỚC QUAN TRỌNG: Phải run ingest + replicate TRƯỚC tấn công, nếu không failover.py sẽ die ở step 2_restore_snapshot.**

- [ ] **Khởi động ingest + replication (chạy nền):**
  ```bash
  python3 state/ingest.py --region a --rate 0.5 --duration 150 &
  python3 state/replicate.py --every 30 --duration 150 --backend fs &
  sleep 5   # đợi cycle replication đầu tiên xong
  ```
  - Output: `reports/replication.jsonl` (log mỗi cycle replicate)
  - Ghi chú: `replicate.py` cần ≥ 1 lần ghi snapshot thành công, nếu không `failover.py` sẽ không có snapshot để restore

- [ ] **Chạy full drill với DR:**
  ```bash
  python3 loadgen/traffic.py --duration 100 --rps 2 --out reports/drill-2-withdr.jsonl &
  python3 dr/health_checker.py --interval 5 --threshold 3 --duration 100 --out reports/health-events.jsonl &
  sleep 12
  python3 chaos/kill_region.py --region a --mode netblock --mock
  python3 dr/runbook.py --primary a --target b --backend fs --auto
  wait
  ```
  - Output files:
    - `reports/drill-2-withdr.jsonl` — every request timestamp + ok/fail
    - `reports/health-events.jsonl` — health check state changes
    - `reports/failover-events.jsonl` — 5 failover steps
    - `reports/runbook-run.jsonl` — 7 runbook steps
    - `chaos/chaos-events.jsonl` — kill event timestamp

- [ ] **Đo RTO từ log (lần đầu xem kết quả):**
  ```bash
  python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300
  ```
  - Output: `reports/measure-drill-2.json` (kết quả tính toán RTO/RPO)
  - Kỳ vọng: `rto_verdict: PASS`, `rto_measured_s < 300`, `valid: true`

- [ ] **Test — RTO ≤ 300s:**
  ```bash
  pytest tests/test_rto_evidence.py::test_drill2_hop_le -v
  ```
  - Kỳ vọng: `valid=true`, `warnings=[]` (empty list)
  - **Điểm: 20 điểm** ✓

- [ ] **Test — RTO đo từ timestamp thật:**
  ```bash
  pytest tests/test_rto_evidence.py::test_rto_do_duoc_bang_timestamp -v
  ```
  - Kỳ vọng: `rto_measured_s` không null, recovered_by_region != killed_region
  - **Điểm: (bao gồm trong 20 điểm trên)** ✓

- [ ] **Test — RPO đo (seconds + documents):**
  ```bash
  pytest tests/test_failover.py::test_rpo_dem_dung_so_doc_bi_mat -v
  pytest tests/test_rto_evidence.py::test_rpo_duoc_do_chu_khong_uoc_luong -v
  ```
  - Kỳ vọng: `rpo_at_restore_s` != null, `docs_lost` != null
  - **Điểm: 10 điểm** ✓

---

## BƯỚC 5 — FILL REPORTS (20 phút) — 20 điểm

### 5a. reports/rto-evidence.md — 10 điểm

**File cần điền:** `reports/rto-evidence.md`

**Yêu cầu:** Mỗi con số PHẢI trỏ được về 1 dòng log thật (path:line). Không có evidence = trượt.

**Điền như sau:**

- [ ] **Section "1. Drill 1 — không có DR":**
  | Chỉ số | Giá trị | Evidence |
  |---|---|---|
  | t_outage | ISO8601 timestamp từ chaos-events.jsonl:1 | `chaos/chaos-events.jsonl:1` |
  | Request fail đầu tiên | `+__s` từ first ok:false sau outage | `reports/drill-1-nodr.jsonl:__` |
  | Request thành công sau | không có | `reports/measure-drill-1.json` |
  | RTO verdict | NO_RECOVERY | `reports/measure-drill-1.json` |

- [ ] **Section "2. Drill 2 — có DR" — 7 mốc:**
  - t_outage (0s): ISO từ chaos-events.jsonl
  - User thấy lỗi: dòng ok:false đầu tiên trong drill-2-withdr.jsonl, +X.Xs từ t_outage
  - Health check phát hiện: dòng `to:UNHEALTHY, region:a` trong health-events.jsonl, +Y.Ys
  - Snapshot restore xong: dòng `step:2_restore_snapshot` trong failover-events.jsonl, +Z.Zs
  - Region phụ ready: dòng `step:4_wait_ready` trong failover-events.jsonl, +A.As
  - DNS cutover: dòng `step:5_dns_cutover` trong failover-events.jsonl, +B.Bs
  - **RTO đo được: dòng ok:true đầu tiên sau lỗi** (trong drill-2-withdr.jsonl), +C.Cs ← **Đây là RTO cuối cùng**

- [ ] **Section "3. RTO breakdown" — 4 thành phần (BẮTBUỘC, không tuỳ chọn):**
  | Thành phần | Giây | Ghi chú |
  |---|---|---|
  | Health-check detect floor | `interval_s × threshold` = ? | `reports/health-events.jsonl:X` (xem interval_s + threshold) |
  | Snapshot restore | `t_2_restore - t_1_verify` = ? | `reports/failover-events.jsonl` (bước 1 đến 2) |
  | GPU pool warm-up | `waited_s` ở bước 4 = ? | `reports/failover-events.jsonl:Y` (bước 4_wait_ready) |
  | DNS/LB TTL cache | `t_rto_final - t_dns_cutover` = ? | `reports/drill-2-withdr.jsonl` (first ok:true sau cutover) |
  | **TỔNG** | sum = ? | **Phải ≈ RTO đo được** |

- [ ] **Test — Evidence trỏ đúng file/line:**
  ```bash
  pytest tests/test_rto_evidence.py::test_evidence_table_tro_vao_file_that -v
  ```
  - **Điểm: (bao gồm trong 10 điểm trên)** ✓

- [ ] **Test — Không còn placeholder:**
  ```bash
  pytest tests/test_rto_evidence.py::test_evidence_table_da_dien_that -v
  ```
  - Kỳ vọng: Không có `TEMPLATE`, không có `__`, không có `+__s`
  - **Điểm: (bao gồm trong 10 điểm trên)** ✓

- [ ] **Test — Con số khớp log (HARD-FAIL nếu sai > 1s):**
  ```bash
  pytest tests/test_rto_evidence.py::test_so_trong_bang_khop_voi_so_trong_log -v
  ```
  - Kỳ vọng: RTO trong bảng ≈ RTO tính từ log (sai ≤ 1s), RPO cũng vậy
  - **Điểm: (bao gồm trong 10 điểm trên)** ✓

- [ ] **Test — Health check config ghi lại:**
  ```bash
  pytest tests/test_rto_evidence.py::test_health_check_interval_duoc_ghi_lai -v
  ```
  - Kỳ vọng: health_events.jsonl có `interval_s` và `threshold`
  - **Điểm: (bao gồm trong 10 điểm trên)** ✓

---

### 5b. reports/postmortem.md — 10 điểm

**File cần điền:** `reports/postmortem.md`

**Yêu cầu:** Blameless postmortem (câu hỏi là "hệ thống cho phép điều gì", không phải "ai làm sai").

**Điền theo template §4:**

- [ ] **Section "1. Timeline" — 5 sự kiện:**
  | ISO time | Sự kiện | Evidence |
  |---|---|---|
  | `t_outage` | Outage bắt đầu (kill signal) | `chaos/chaos-events.jsonl:__` |
  | `t_first_error` | User đầu tiên bị ảnh hưởng (ok:false) | `reports/drill-2-withdr.jsonl:__` |
  | `t_health_alert` | Health check phát hiện UNHEALTHY | `reports/health-events.jsonl:__` |
  | `t_operator_confirm` | Operator confirm cutover (runbook step 2) | `reports/runbook-run.jsonl:__` |
  | `t_resolved` | Resolved (first ok:true từ region phụ) | `reports/drill-2-withdr.jsonl:__` |
  - Mỗi ô phải có evidence path:line

- [ ] **Section "2. RTO/RPO vs Target — gap analysis":**
  - RTO mục tiêu: 300s (từ GUIDE §1)
  - RTO đo được: `__s` (từ measure-drill-2.json)
  - Gap: `__s` (mục tiêu - đo được)
  - RPO mục tiêu: 300s
  - RPO đo được: `__s` / `__` doc bị mất
  - Gap: `__s`
  - **Bước tốn nhiều giây nhất:** `____` (health-check detect floor? restore? warmup? TTL cache?) — vì sao?

- [ ] **Section "3. Root cause (5 whys)" — Blameless:**
  - KHÔNG viết "vì tôi chạy chaos script"
  - ĐỌC: "nếu đây là outage thật (chứ không phải mô phỏng), bước nào trong runbook của tôi sẽ thất bại?"
  - Ví dụ: "health check detection floor là 15s vì interval=5s × threshold=3 → phải poll 3 lần liên tiếp fail"
  - Ví dụ: "snapshot restore mất 2s vì phải đọc SQLite từ disk + copy weights file"

- [ ] **Section "4. Action items" — Owner + Deadline + Impact:**
  | # | Action | Owner | Deadline | Giảm RTO (s) |
  |---|---|---|---|---|
  | 1 | Giảm health check interval từ 5s → 3s? | SRE | 1 tuần | 4–5s |
  | 2 | ...? | ? | ? | ? |
  - Ghi chú: Mỗi action phải có owner cụ thể (không phải "team")

- [ ] **Section "5. Ba câu hỏi bắt buộc trả lời":**
  1. `interval × threshold` của bạn = `__s` (e.g., 5 × 3 = 15s). Nó chiếm `__`% RTO? (e.g., 15/28 = 53%)
  2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì? (Gợi ý: flapping risk từ network jitter)
  3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?
     - (Cần suy nghĩ: RPO tiêu chuẩn, SLA commitment, customer impact)

- [ ] **Test — Postmortem đủ yếu tố:**
  ```bash
  pytest tests/test_rto_evidence.py::test_postmortem_co_gap_analysis -v
  ```
  - Kỳ vọng: Có `rto`, `rpo`, `gap`, `action item` (case-insensitive)
  - **Điểm: 10 điểm** ✓

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
