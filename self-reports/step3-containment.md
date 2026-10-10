# Báo cáo BƯỚC 3 — CONTAINMENT

## S1 (3a) — dr/health_checker.py

**Thời gian:** 2026-10-10 21:15 (+07)

### Quyết định thiết kế

- **`probe()` function**: Poll HTTP GET `/readyz` endpoint (KHÔNG `/healthz` vì /healthz chỉ check process còn sống, không check vector DB hay model weights)
- **Timeout bắt buộc**: Dùng `httpx.get(..., timeout=timeout)` để chặn netblock trap (SIGSTOP trong chaos test)
- **Return tuple**: `(ready: bool, reason: str)` để tracking lý do nếu fail
- **Error handling**: Catch `TimeoutException`, `ConnectError`, và generic exceptions để đảm bảo test được monkeypatch

- **`run()` function**: Vòng lặp poll cả 2 region mỗi `interval` giây
  - Trạng thái: HEALTHY (mặc định) → UNHEALTHY (sau >= threshold lần fail liên tiếp)
  - Đếm consecutive failures riêng cho mỗi region
  - **Chỉ ghi JSONL khi state thay đổi** (HEALTHY → UNHEALTHY), KHÔNG ghi mỗi lần poll
  - **Field bắt buộc**: ts, iso (ISO8601), event="state_change", region, to, reason, interval_s, threshold, consecutive_fails
  - Reset counter về 0 khi probe thành công

### Điểm đáng chú ý

1. **Anti-flapping**: Yêu cầu `consecutive_fails >= threshold` để flip state, không phải 1 lần fail
2. **ISO timestamp**: Tính toán từ Unix ts dùng `time.gmtime(ts)` + `time.strftime`
3. **Không import từ serving/**: Hoàn toàn độc lập, chỉ dùng httpx
4. **JSONL append mode**: Mở file với `"a"` (append), không overwrite

### Lệnh test

```bash
pytest tests/test_failover.py::test_health_checker_can_threshold_lien_tiep -v
```

### Output

```
tests/test_failover.py::test_health_checker_can_threshold_lien_tiep PASSED [100%]
```

### Lệch từ CHECKLIST

Không có lệch. Docstring + test + code thật đều khớp.

