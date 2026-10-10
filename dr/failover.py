"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    ts = time.time()
    kw["ts"] = ts
    kw["iso"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))

    line = json.dumps(kw)
    print(line)

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước failover: verify -> restore -> scale -> wait -> cutover."""
    try:
        # Step 1: Verify target state
        resp = httpx.get(f"{URL[target]}/v1/state", timeout=5.0)
        resp.raise_for_status()
        state_data = resp.json()

        emit(
            step="1_verify_target",
            region=target,
            pool_state=state_data.get("pool_state"),
            vector_count=state_data.get("count"),
            weights=state_data.get("weights")
        )

        # Step 2: Restore snapshot
        meta = snapshot.get(target, backend)
        rpo_data = snapshot.rpo(
            pathlib.Path(f"state/region-a/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite")
        )

        emit(
            step="2_restore_snapshot",
            region=target,
            rpo_seconds=rpo_data.get("rpo_seconds"),
            docs_lost=rpo_data.get("docs_lost"),
            embed_model_version=meta.get("embed_model_version")
        )

        # Step 3: Scale pool from warm to full
        pool_state_file = pathlib.Path(f"state/region-{target}/pool_state")
        pool_state_file.parent.mkdir(parents=True, exist_ok=True)
        pool_state_file.write_text("full\n")

        emit(
            step="3_scale_pool",
            region=target,
            to="full"
        )

        # Step 4: Wait for target to be ready
        start = time.time()
        ready = False
        waited_s = 0.0

        while time.time() - start < wait:
            try:
                resp = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ready"):
                        ready = True
                        waited_s = round(time.time() - start, 2)
                        break
            except Exception:
                pass
            time.sleep(0.5)

        if not ready:
            # Timeout: abort failover, do NOT cutover
            emit(
                step="4_wait_ready",
                region=target,
                status="timeout",
                waited_s=round(time.time() - start, 2)
            )
            return {"ok": False, "reason": "target_timeout"}

        emit(
            step="4_wait_ready",
            region=target,
            status=200,
            waited_s=waited_s
        )

        # Step 5: DNS cutover
        active_file = pathlib.Path("edge/active_region")
        active_file.parent.mkdir(parents=True, exist_ok=True)
        active_file.write_text(target + "\n")

        emit(
            step="5_dns_cutover",
            active_region=target
        )

        return {"ok": True, "rpo_seconds": rpo_data.get("rpo_seconds"), "docs_lost": rpo_data.get("docs_lost")}

    except Exception as e:
        return {"ok": False, "reason": str(e)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
