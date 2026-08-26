"""v0.4.1 术语表批量导入：合并/校验/行号定位。零网络。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.glossary_io import collect_yaml_files, validate_and_merge, validate_text


def _write(tmp: Path, name: str, content: str) -> Path:
    p = tmp / name
    p.write_text(content, encoding="utf-8")
    return p


def test_merge_later_wins(tmp_path):
    a = _write(tmp_path, "a.yaml", "Chern number: 陈数\nBerry curvature: 贝里曲率\n")
    b = _write(tmp_path, "b.yaml", "Chern number: 陈数(新)\nquantum Hall: 量子霍尔\n")
    merged, issues = validate_and_merge([str(a), str(b)])
    assert merged["Chern number"] == "陈数(新)"   # 后者覆盖
    assert merged["Berry curvature"] == "贝里曲率"
    assert len(merged) == 3
    warns = [i for i in issues if i["kind"] == "warning"]
    assert any("后者覆盖" in w["msg"] for w in warns)


def test_syntax_error_line_number(tmp_path):
    bad = _write(tmp_path, "bad.yaml", "good: 好\n  badIndent: x\n")
    merged, issues = validate_and_merge([str(bad)])
    assert merged is not None and issues
    err = next(i for i in issues if i["kind"] == "error")
    assert err["line"] == 2


def test_bad_value_types(tmp_path):
    f = _write(tmp_path, "t.yaml",
               "list_val: [a, b]\nnum_val: 123\nbool_val: true\nok: 正常\n")
    merged, issues = validate_and_merge([str(f)])
    errors = {i["key"] for i in issues if i["kind"] == "error"}
    assert errors == {"list_val", "num_val", "bool_val"}
    assert "ok" in merged


def test_empty_and_missing_file(tmp_path):
    empty = _write(tmp_path, "e.yaml", "")
    merged, issues = validate_and_merge([str(empty), str(tmp_path / "nope.yaml")])
    kinds = [i["kind"] for i in issues]
    assert "warning" in kinds and "error" in kinds
    assert merged == {}


def test_non_mapping_root(tmp_path):
    f = _write(tmp_path, "arr.yaml", "- a\n- b\n")
    _, issues = validate_and_merge([str(f)])
    assert issues[0]["kind"] == "error"
    assert "平面映射" in issues[0]["msg"]


def test_validate_text_save_path(tmp_path):
    ok_text = "Chern number: 陈数\nBerry curvature: 贝里曲率\n"
    merged, issues = validate_text(ok_text)
    assert merged and not issues and len(merged) == 2
    bad_text = "A: 123\nB: [x]\n"
    merged2, issues2 = validate_text(bad_text)
    assert merged2 is not None   # 部分有效也回传
    assert any(i["kind"] == "error" for i in issues2)


def test_collect_recursive_depth(tmp_path):
    (tmp_path / "sub" / "deep").mkdir(parents=True)
    _write(tmp_path, "r1.yaml", "a: b\n")
    _write(tmp_path / "sub", "r2.yml", "c: d\n")
    _write(tmp_path / "sub" / "deep", "r3.json", '{"e": "f"}')
    _write(tmp_path / "sub" / "deep", "skip.txt", "x")
    files = collect_yaml_files(str(tmp_path), recursive=True, depth=2)
    names = sorted(Path(f).name for f in files)
    assert names == ["r1.yaml", "r2.yml", "r3.json"]
    # depth=1 时 deep 层被排除
    files1 = collect_yaml_files(str(tmp_path), recursive=True, depth=1)
    assert sorted(Path(f).name for f in files1) == ["r1.yaml", "r2.yml"]
    # 非递归只取顶层
    files0 = collect_yaml_files(str(tmp_path), recursive=False)
    assert [Path(f).name for f in files0] == ["r1.yaml"]


def test_single_file_collect(tmp_path):
    f = _write(tmp_path, "one.yaml", "a: b\n")
    assert collect_yaml_files(str(f)) == [str(f)]