"""LLM analysis path end-to-end against a local fake OpenAI-compatible server (no external network)."""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest
from test_cli import TRACES, run_cli, workdir

from faillab.store import Store

ANALYZE_PAYLOAD = {
    "category": "env_permission",
    "reason": "假服务器：错误显示命令不存在，属于环境缺失",
    "recovery": "安装缺失命令并确认 PATH",
    "severity": "medium",
    "confidence": "high",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    mode = "ok"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        text = json.dumps(body, ensure_ascii=False)
        content = json.dumps(ANALYZE_PAYLOAD, ensure_ascii=False) if "失败分析助手" in text else "{}"
        if type(self).mode == "garbage":
            content = "这不是 JSON"
        data = json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def fake_llm():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    _Handler.mode = "ok"
    yield base_url
    server.shutdown()
    server.server_close()


def _llm_env(base_url: str) -> dict[str, str]:
    return {
        "FAILLAB_LLM_BASE_URL": base_url,
        "FAILLAB_LLM_MODEL": "fake-qwen",
        "FAILLAB_LLM_API_KEY": "test-key",
    }


def test_analyze_with_llm_backend(fake_llm, tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    code, output = run_cli(
        "analyze",
        "F-0001",
        "--backend",
        "llm",
        "--workdir",
        workdir(tmp_path),
        cwd=tmp_path,
        env_extra=_llm_env(fake_llm),
    )
    assert code == 0, output
    assert "环境/权限问题" in output  # 假服务器给的类别
    assert "llm" in output
    with Store(Path(workdir(tmp_path)) / "faillab.db") as store:
        incident = store.require_incident("F-0001")
        assert incident.analyzed_by is not None and incident.analyzed_by.value == "llm"
        assert incident.fallback_reason is None


def test_analyze_llm_garbage_falls_back(fake_llm, tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    _Handler.mode = "garbage"
    code, output = run_cli(
        "analyze",
        "F-0001",
        "--backend",
        "llm",
        "--workdir",
        workdir(tmp_path),
        cwd=tmp_path,
        env_extra=_llm_env(fake_llm),
    )
    assert code == 0, output
    assert "LLM 降级" in output
    with Store(Path(workdir(tmp_path)) / "faillab.db") as store:
        incident = store.require_incident("F-0001")
        assert incident.category == "wrong_file_edit"  # 离线规则兜底
        assert incident.fallback_reason is not None


def test_analyze_llm_without_key_is_structured_error(tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    code, output = run_cli(
        "analyze",
        "F-0001",
        "--backend",
        "llm",
        "--workdir",
        workdir(tmp_path),
        cwd=tmp_path,
        strip_llm_env=True,
    )
    assert code == 3
    assert "LLM_DISABLED" in output


def test_analyze_auto_degrades_with_note(tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    code, output = run_cli(
        "analyze",
        "F-0001",
        "--backend",
        "auto",
        "--workdir",
        workdir(tmp_path),
        cwd=tmp_path,
        strip_llm_env=True,
    )
    assert code == 0
    assert "auto 降级为离线后端" in output
