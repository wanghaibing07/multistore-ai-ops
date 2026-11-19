import os

import pytest

from app.importer import ImportProblem
from app.main import latest_download_file


def test_latest_download_rejects_unrelated_spreadsheet(tmp_path, monkeypatch):
    (tmp_path / "unrelated.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    monkeypatch.setenv("MULTISTORE_DOWNLOADS_DIR", str(tmp_path))

    with pytest.raises(ImportProblem, match="匹配当前导入类型"):
        latest_download_file("orders")


def test_latest_download_prefers_matching_file(tmp_path, monkeypatch):
    matching = tmp_path / "千牛订单.csv"
    unrelated = tmp_path / "unrelated.xlsx"
    matching.write_bytes(b"matching")
    unrelated.write_bytes(b"unrelated")
    os.utime(matching, ns=(1_000_000_000, 1_000_000_000))
    os.utime(unrelated, ns=(2_000_000_000, 2_000_000_000))
    monkeypatch.setenv("MULTISTORE_DOWNLOADS_DIR", str(tmp_path))

    assert latest_download_file("orders") == matching
