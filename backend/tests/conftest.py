"""共享测试夹具。"""
from __future__ import annotations

import pytest


@pytest.fixture()
def isolate_ta_factors(monkeypatch):
    """把第三方 TA 因子 (可选 extra `ta`) 从全局注册表隔离。

    内置因子目录的计数/顺序黄金断言 (test_factor_registry / test_factor_store) 必须与
    可选 extra 的安装状态无关: 安装 extra 时 registry.all_factors() 会惰性注册 ta_ 前缀
    条目, 未隔离会改变内置计数。此夹具令 ta_factors.available() 恒为 False 并清掉已泄漏
    的 ta_ 条目, 使黄金断言在两种环境下都确定。
    """
    from app.factors import ta_factors
    from app.factors.registry import _REGISTRY

    def _purge() -> None:
        for fid in [k for k in list(_REGISTRY) if k.startswith(ta_factors.TA_PREFIX)]:
            _REGISTRY.pop(fid, None)
        ta_factors._registered = False

    monkeypatch.setattr(ta_factors, "available", lambda: False)
    _purge()
    yield
    _purge()
