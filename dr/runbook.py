"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    ts = time.time()
    kw["ts"] = ts
    kw["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))
    kw["step"] = n
    kw["name"] = name

    line = json.dumps(kw)
    print(line)

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N."""
    if auto:
        return True
    resp = input(msg + " (y/N): ").strip().lower()
    return resp == "y"


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook automation."""
    LOG.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: Xác nhận outage
    # Probe primary tối đa 3 lần
    fails = 0
    for _ in range(3):
        try:
            resp = httpx.get(f"{URL[primary]}/readyz", timeout=2.0)
            if resp.status_code == 200 and resp.json().get("ready"):
                fails = 0
                break
        except Exception:
            fails += 1
        time.sleep(0.1)

    # Ghi ĐÚNG 1 dòng bước 1 SAU khi probe
    # alive=True nếu có lần nào ready (fails=0), False nếu cả 3 lần fail (fails=3)
    step(1, "xac_nhan_outage", alive=(fails == 0), probes_failed=fails)

    # Step 2: Thông báo incident
    t_outage_from_chaos = None
    try:
        chaos_events = pathlib.Path("chaos/chaos-events.jsonl").read_text().splitlines()
        if chaos_events:
            last_kill = json.loads(chaos_events[-1])
            if last_kill.get("action") == "kill":
                t_outage_from_chaos = last_kill.get("ts")
    except Exception:
        pass

    t_incident = time.time()
    step(2, "thong_bao_incident", t_outage_from_chaos=t_outage_from_chaos)

    if not confirm(auto, "Proceed with failover?"):
        return {"ok": False, "reason": "operator_declined"}

    # Step 3: Scale GPU pool (call failover ONCE)
    failover_result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", ok=failover_result.get("ok"),
         reason=failover_result.get("reason"))

    if not failover_result.get("ok"):
        return {"ok": False, "reason": failover_result.get("reason")}

    # Step 4: Verify state replica
    try:
        state_file = pathlib.Path(f"state/region-{target}/vectors.sqlite")
        replica_count = 0
        if state_file.exists():
            import sqlite3
            con = sqlite3.connect(f"file:{state_file}?mode=ro", uri=True)
            try:
                replica_count = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
            finally:
                con.close()

        weights_file = pathlib.Path(f"state/region-{target}/weights/model.bin")
        weights_restored = weights_file.exists()

        step(4, "verify_state_replica", vector_count=replica_count,
             weights_restored=weights_restored)
    except Exception as e:
        step(4, "verify_state_replica", error=str(e))

    # Step 5: DNS cutover (verify from failover result)
    try:
        active_region = pathlib.Path("edge/active_region").read_text().strip()
        step(5, "dns_cutover", active_region=active_region,
             cutover_ok=(active_region == target))
    except Exception as e:
        step(5, "dns_cutover", error=str(e))

    # Step 6: Verify golden signals (10 requests)
    errors = 0
    latencies = []
    for _ in range(10):
        try:
            start = time.time()
            resp = httpx.get("http://127.0.0.1:8080/v1/infer", timeout=5.0)
            latency = (time.time() - start) * 1000
            latencies.append(latency)
            if resp.status_code != 200:
                errors += 1
        except Exception:
            errors += 1

    p95_ms = None
    if latencies:
        latencies.sort()
        idx = int(len(latencies) * 0.95)
        p95_ms = round(latencies[min(idx, len(latencies)-1)], 1)

    error_rate = round(errors / 10.0 * 100, 1)
    step(6, "verify_golden_signals", requests_sent=10, errors=errors, p95_ms=p95_ms,
         error_rate=error_rate)

    # Step 7: Post incident
    elapsed_s = round(time.time() - t_incident, 1)
    step(7, "post_incident", elapsed_s=elapsed_s,
         recommend_next="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")

    return {"ok": True}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
