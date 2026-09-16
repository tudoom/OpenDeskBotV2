"""内部分发版在企业网络里免手工配置（2026-09-14）：种子目录可带附加文件（如企业 CA 证书包）随 .env 一起
首启落地；.env 里 SSL_CERT_FILE / REQUESTS_CA_BUNDLE 写相对路径时按 .env 所在目录解析。代码里不含任何企业信息。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_relative_ca_bundle_path_resolves_next_to_env(tmp_path, monkeypatch):
    from deskbot_server import env as env_module

    (tmp_path / "ca-bundle.pem").write_text("cert", encoding="utf-8")
    envf = tmp_path / ".env"
    envf.write_text(
        'SSL_CERT_FILE=ca-bundle.pem\nREQUESTS_CA_BUNDLE="ca-bundle.pem"\nCURL_CA_BUNDLE=missing.pem\n',
        encoding="utf-8",
    )
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(env_module, "ENV_FILE", envf)
    monkeypatch.setattr(env_module, "_file_managed_values", {})
    monkeypatch.setattr(env_module, "_last_signature", None)
    assert env_module.load_dotenv(force_reload=True) is True
    resolved = str((tmp_path / "ca-bundle.pem").resolve())
    assert os.environ["SSL_CERT_FILE"] == resolved
    assert os.environ["REQUESTS_CA_BUNDLE"] == resolved
    assert os.environ["CURL_CA_BUNDLE"] == "missing.pem"  # 文件不存在：原样保留，交给下游报错
    # 绝对路径不动
    envf.write_text("SSL_CERT_FILE=/etc/ssl/cert.pem\n", encoding="utf-8")
    assert env_module.load_dotenv(force_reload=True) is True
    assert os.environ["SSL_CERT_FILE"] == "/etc/ssl/cert.pem"


def test_seed_extra_files_contract():
    swift = (ROOT / "service/client-macos/main.swift").read_text(encoding="utf-8")
    cs = (ROOT / "service/client/OpenDeskBotV2Launcher.cs").read_text(encoding="utf-8")
    sh = (ROOT / "service/client-macos/build-client-macos.sh").read_text(encoding="utf-8")
    ps = (ROOT / "service/client/Build-Client.ps1").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    # 两端启动器：seed 顶层的其它文件首启落地，已有文件不覆盖，三个固定文件仍走原逻辑
    assert 'skipping: ["config.yaml", ".env.example", ".env"]' in swift and "copyTopLevelFilesIfMissing" in swift
    assert "SearchOption.TopDirectoryOnly" in cs and 'name == ".env.example"' in cs and "CopyFileIfMissing(extra" in cs
    # 两端打包脚本：只有内部分发（带种子 .env）才会带 client/seed.d/
    assert 'SEED_EXTRA_DIR="$SERVICE_ROOT/client/seed.d"' in sh and sh.index("seed.d") > sh.index("已打入种子 .env")
    assert 'Join-Path $clientRoot "seed.d"' in ps and ps.index("seed.d") > ps.index("Seeded runtime credentials")
    # 附加文件目录和种子 .env 一样绝不入库
    assert "service/client/seed.env\n" in gitignore and "service/client/seed.d/\n" in gitignore
