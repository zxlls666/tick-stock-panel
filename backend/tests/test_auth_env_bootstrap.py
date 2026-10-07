from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from app import config as app_config
from app.config import Settings


@pytest.fixture(autouse=True)
def isolated_auth_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[ModuleType, Path, Path]]:
    # 隔离真实 data_dir: 既要覆盖已构造的 settings 单例, 也要让任何**新建**的
    # Settings() 解析到 tmp_path。进程环境通常已带 DATA_DIR (如 .env 的 ./data),
    # 它会覆盖构造参数 _env_file —— 只 setattr 单例无法隔离"被测代码替换 settings"。
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    from app.services import auth

    auth_path = tmp_path / "user_data" / "auth.json"
    env_path = tmp_path / ".env"
    monkeypatch.setattr(app_config, "_ENV_FILE", env_path)
    monkeypatch.setattr(app_config.settings, "auth_password", "")
    auth._sessions.clear()
    auth._configured_cache = None
    yield auth, auth_path, env_path
    auth._sessions.clear()
    auth._configured_cache = None


def test_bootstrap_recovers_compose_interpolated_password_from_raw_env(
    isolated_auth_store: tuple[ModuleType, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    auth, auth_path, env_path = isolated_auth_store
    password = "pw${special}-secret"
    env_path.write_text(
        f"AUTH_PASSWORD={password}\nDATA_DIR={tmp_path}\n",
        encoding="utf-8",
    )
    configured = Settings(_env_file=env_path)
    configured.auth_password = "pw-secret"  # 模拟 Compose 将未定义的 ${special} 插值为空串
    monkeypatch.setattr(app_config, "settings", configured)
    # 隔离断言: 替换 settings 单例后 auth 仍必须落在 tmp 目录 (回归: 曾泄漏到真实 data_dir)
    assert auth._path() == tmp_path / "user_data" / "auth.json"

    assert auth.bootstrap_from_env() is True
    assert auth_path.exists()
    assert password not in auth_path.read_text(encoding="utf-8")
    assert auth.verify_and_create_session(password) is not None
    assert auth.verify_and_create_session("pw-secret") is None


def test_bootstrap_from_env_does_not_override_existing_password(
    isolated_auth_store: tuple[ModuleType, Path, Path],
) -> None:
    auth, auth_path, env_path = isolated_auth_store
    auth.set_password("web-managed-secret")
    before = auth_path.read_bytes()
    env_path.write_text("AUTH_PASSWORD=replacement-secret\n", encoding="utf-8")
    app_config.settings.auth_password = "replacement-secret"

    assert auth.bootstrap_from_env() is False
    assert auth_path.read_bytes() == before
    assert auth.verify_and_create_session("web-managed-secret") is not None
    assert auth.verify_and_create_session("replacement-secret") is None


def test_auth_bootstrap_never_writes_real_data_dir(
    isolated_auth_store: tuple[ModuleType, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """回归: 替换 settings 单例后, bootstrap 也不得写到真实 data_dir。

    复现旧缺陷: 测试用新 Settings() 替换 app_config.settings, 而进程环境里已有
    DATA_DIR(真实目录), 导致 auth._path() 指向真实 auth.json 并被写入 —— 跑一次
    测试就会覆盖用户的访问密码。
    """
    auth, auth_path, env_path = isolated_auth_store
    real_auth = app_config._user_data_root() / "user_data" / "auth.json"
    before = real_auth.read_bytes() if real_auth.exists() else None

    env_path.write_text("AUTH_PASSWORD=leak-check-secret\n", encoding="utf-8")
    configured = Settings()
    configured.auth_password = "leak-check-secret"
    monkeypatch.setattr(app_config, "settings", configured)  # 故意替换单例

    assert auth._path() == tmp_path / "user_data" / "auth.json"
    assert auth.bootstrap_from_env() is True
    assert auth_path.exists()

    after = real_auth.read_bytes() if real_auth.exists() else None
    assert after == before, "bootstrap 写到了真实 data_dir (隔离失效)"
