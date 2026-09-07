"""FuyaoClient._get HTTP 429 退避重试测试。

不依赖真实网络: 用 stub 替换 httpx 响应序列, 验证
- 429 → 指数退避重试 → 成功后正常返回 data;
- 429 持续到重试耗尽 → 仍抛 FuyaoError;
- 非 429 状态码不重试, 直接抛错;
- 网络异常不重试, 直接抛错。
"""

from __future__ import annotations

import pytest

from app.plugins.fuyao import client as fc


class _StubResp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _StubHttp:
    """按顺序弹出预置响应; 全部弹出后抛 httpx.HTTPError(模拟网络断)。"""

    def __init__(self, responses: list[_StubResp]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, path: str, params: dict):
        self.calls.append({"path": path, "params": params})
        if not self.responses:
            import httpx
            raise httpx.ConnectError("stub: no more responses")
        return self.responses.pop(0)


def _client_with(stub_http: _StubHttp) -> fc.FuyaoClient:
    c = fc.FuyaoClient(api_key="test-key")
    c._http = stub_http  # noqa: SLF001 — 测试替换内部客户端
    return c


def test_429_retries_then_succeeds(monkeypatch):
    """429 → 退避 1s → 重试 200: 调用 2 次, 返回 data, 退避被 sleep。"""
    slept: list[tuple[float, object]] = []

    def fake_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr(fc.time, "sleep", fake_sleep)
    http = _StubHttp([
        _StubResp(429, {"code": 429}),
        _StubResp(200, {"code": 0, "data": {"item": [{"x": 1}]}}),
    ])
    c = _client_with(http)

    data = c._get("/api/a-share/financials/indicators", {"thscode": "600519.SH"})
    assert data == {"item": [{"x": 1}]}
    assert len(http.calls) == 2
    assert slept == [1.0]  # 首退避 1s


def test_429_exhausts_retries_raises(monkeypatch):
    """429 连返 (重试耗尽): 调用 1+3 次, 退避 1/2/4s, 最终抛 HTTP 429。"""
    slept: list[float] = []
    monkeypatch.setattr(fc.time, "sleep", lambda s: slept.append(s))
    http = _StubHttp([_StubResp(429, {}) for _ in range(1 + fc._HTTP_429_RETRIES)])
    c = _client_with(http)

    with pytest.raises(fc.FuyaoError, match="HTTP 429"):
        c._get("/api/a-share/financials/indicators", {"thscode": "x"})
    assert len(http.calls) == 1 + fc._HTTP_429_RETRIES
    assert slept == [1.0, 2.0, 4.0]


def test_non_429_no_retry(monkeypatch):
    """500 只调一次, 不重试, 直接抛 HTTP 500。"""
    monkeypatch.setattr(fc.time, "sleep", lambda s: pytest.fail("不应退避"))
    http = _StubHttp([_StubResp(500, {})])
    c = _client_with(http)

    with pytest.raises(fc.FuyaoError, match="HTTP 500"):
        c._get("/api/a-share/prices/snapshot", {})
    assert len(http.calls) == 1


def test_network_error_no_retry():
    """首请求即网络异常: 不重试, 直接抛网络失败。"""
    import httpx

    class _Broken:
        def get(self, path, params):
            raise httpx.ConnectError("boom")

    c = _client_with(_Broken())  # type: ignore[arg-type]
    with pytest.raises(fc.FuyaoError, match="网络请求失败"):
        c._get("/api/a-share/prices/snapshot", {})