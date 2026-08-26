"""术语表批量导入：多文件合并 + 格式/语法校验（行号级错误定位）。

设计（2026-08-26 与用户拍板）：
- 合并语义：按传入顺序后者覆盖前者
- 校验粒度：yaml.compose 拿 AST → 错误带行号（yaml.safe_load 的异常
  problem_mark 也能给行号；但格式类错误（值不是字符串等）safe_load 不报，
  必须自己遍历节点
- error 阻止保存；warning（重复定义、空文件）不阻止
"""
from __future__ import annotations

from pathlib import Path

import yaml

YAML_EXTS = {".yaml", ".yml", ".json"}


class GlossaryIssue:
    def __init__(self, file: str, line: int, kind: str, key: str,
                 msg: str) -> None:
        self.file = file      # 相对路径展示
        self.line = line      # 1-based；无法定位时 -1
        self.kind = kind      # 'error' | 'warning'
        self.key = key        # 涉及术语；无关时 ''
        self.msg = msg

    def to_dict(self) -> dict:
        return {"file": self.file, "line": self.line, "kind": self.kind,
                "key": self.key, "msg": self.msg}


def collect_yaml_files(path: str, recursive: bool = True,
                       depth: int = 2) -> list[str]:
    """目录下 .yaml/.yml/.json 文件。recursive 时向下递归 depth 层。"""
    p = Path(path)
    if p.is_file():
        return [str(p)] if p.suffix.lower() in YAML_EXTS else []
    if not p.is_dir():
        return []
    out: list[str] = []
    if not recursive:
        return sorted(str(f) for f in p.glob("*")
                      if f.is_file() and f.suffix.lower() in YAML_EXTS)
    stack = [(p, 0)]
    while stack:
        d, lv = stack.pop()
        if lv > depth:
            continue
        for f in sorted(d.iterdir(), key=lambda x: x.name):
            if f.is_dir():
                if lv < depth:
                    stack.append((f, lv + 1))
            elif f.suffix.lower() in YAML_EXTS:
                out.append(str(f))
    return out


def _node_line(node) -> int:
    try:
        return node.start_mark.line + 1
    except AttributeError:
        return -1


def _scalar_value(node) -> str | None:
    """ScalarNode → 字符串值；非字符串标量（数字/布尔）也转出并标记。"""
    if not isinstance(node, yaml.ScalarNode):
        return None
    return str(node.value)


def validate_and_merge(paths: list[str]) -> tuple[dict, list[dict]]:
    """合并多份术语表，返回 (merged, issues)。

    issues[kind=error] 存在时调用方应拒绝保存；warning 仅提示。
    """
    merged: dict = {}
    issues: list[dict] = []
    seen: dict[str, tuple[str, int]] = {}   # 术语 → (首次来源, 行号)，用于跨文件重复警告

    for raw in paths:
        path = Path(raw)
        name = path.name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            issues.append(GlossaryIssue(name, -1, "error", "",
                                        f"无法读取: {e.strerror or e}").to_dict())
            continue
        try:
            root = yaml.compose(text)
        except yaml.YAMLError as e:
            line = getattr(getattr(e, "problem_mark", None), "line", -1) + 1
            problem = getattr(e, "problem", None) or str(e)
            issues.append(GlossaryIssue(name, line, "error", "",
                                        f"YAML 语法错误: {problem}").to_dict())
            continue
        if root is None:
            issues.append(GlossaryIssue(name, -1, "warning", "",
                                        "空文件，已跳过").to_dict())
            continue
        if not isinstance(root, yaml.MappingNode):
            issues.append(GlossaryIssue(name, _node_line(root), "error", "",
                                        "顶层必须是 {英文原词: 中文译名} 平面映射").to_dict())
            continue
        mapping: dict = {}
        for k_node, v_node in root.value:
            line = _node_line(k_node)
            if not isinstance(k_node, yaml.ScalarNode):
                issues.append(GlossaryIssue(name, line, "error", "",
                                            "术语键不是文本").to_dict())
                continue
            term = k_node.value
            if term in mapping:
                issues.append(GlossaryIssue(name, line, "warning", term,
                                            f"「{term}」在本文件重复定义，后者生效").to_dict())
            if not isinstance(v_node, yaml.ScalarNode):
                issues.append(GlossaryIssue(name, line, "error", term,
                                            f"「{term}」的译名必须是纯文本（当前是嵌套结构）").to_dict())
                continue
            if v_node.tag not in ("tag:yaml.org,2002:str",):
                issues.append(GlossaryIssue(name, line, "error", term,
                                            f"「{term}」的译名不是字符串（数字/布尔需加引号）").to_dict())
                continue
            if term in seen:
                issues.append(GlossaryIssue(name, line, "warning", term,
                                            f"「{term}」另在 {seen[term][0]} 定义，后者覆盖").to_dict())
            else:
                seen[term] = (name, line)
            mapping[term] = v_node.value
        merged.update(mapping)

    return merged, issues


def dump_merged(merged: dict) -> str:
    return yaml.safe_dump(merged, allow_unicode=True, sort_keys=False,
                          default_flow_style=False)


def validate_text(text: str) -> tuple[dict | None, list[dict]]:
    """校验单个文本（保存前整份重验）。返回 (merged 或 None, issues)。"""
    issues: list[dict] = []
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as e:
        line = getattr(getattr(e, "problem_mark", None), "line", -1) + 1
        problem = getattr(e, "problem", None) or str(e)
        return None, [GlossaryIssue("", line, "error", "",
                                    f"YAML 语法错误: {problem}").to_dict()]
    if root is None:
        return {}, []
    if not isinstance(root, yaml.MappingNode):
        return None, [GlossaryIssue("", _node_line(root), "error", "",
                                    "顶层必须是 {英文原词: 中文译名} 平面映射").to_dict()]
    merged: dict = {}
    for k_node, v_node in root.value:
        line = _node_line(k_node)
        if not isinstance(k_node, yaml.ScalarNode) or \
                not isinstance(v_node, yaml.ScalarNode):
            issues.append(GlossaryIssue("", line, "error", k_node.value
                                        if isinstance(k_node, yaml.ScalarNode) else "",
                                        "术语条目必须是「英文: 中文」文本对").to_dict())
            continue
        if v_node.tag not in ("tag:yaml.org,2002:str",):
            issues.append(GlossaryIssue("", line, "error", k_node.value,
                                        f"「{k_node.value}」的译名不是字符串（需加引号）").to_dict())
            continue
        merged[k_node.value] = v_node.value
    return merged, issues