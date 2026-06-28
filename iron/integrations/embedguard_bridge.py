"""EmbedGuard 桥接模块 — 调用 EmbedGuard 静态分析"""
import logging
from pathlib import Path


def analyze_paths(paths: tuple, mcu: str = "") -> list[dict]:
    """对指定路径运行 EmbedGuard 静态分析

    Args:
        paths: 要分析的文件/目录路径
        mcu: 目标 MCU 型号

    Returns:
        findings 列表: [{"rule": "EMB001", "severity": "error", "line": 10, "message": "...", "fixable": True}]
    """
    try:
        from embedguard.core.pipeline import AnalysisPipeline
        from embedguard.core.config import EmbedGuardConfig
    except ImportError:
        logging.warning("EmbedGuard 未安装，请先安装 embedguard 包")
        return []

    config = EmbedGuardConfig()
    if mcu:
        config.target_mcu = mcu

    pipeline = AnalysisPipeline(config)
    all_findings = []

    for path_str in paths:
        p = Path(path_str)
        if p.is_dir():
            for ext in ("*.c", "*.cpp", "*.h", "*.hpp"):
                for f in p.rglob(ext):
                    findings = _analyze_file(pipeline, f, mcu)
                    all_findings.extend(findings)
        elif p.is_file():
            findings = _analyze_file(pipeline, p, mcu)
            all_findings.extend(findings)

    return all_findings


def _analyze_file(pipeline, file_path: Path, mcu: str) -> list[dict]:
    """分析单个文件"""
    try:
        try:
            code = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # 非 UTF-8 文件，回退到 GBK
            code = file_path.read_text(encoding="gbk")
        result = pipeline.analyze(code, target_mcu=mcu, file_name=str(file_path))
        findings = result.findings if hasattr(result, "findings") and result.findings else []
        out = []
        for f in findings:
            out.append({
                "rule": getattr(f, "rule_id", "UNKNOWN"),
                "severity": getattr(f, "severity", "warning"),
                "line": getattr(f, "line", 0),
                "message": getattr(f, "message", ""),
                "fixable": getattr(f, "fixable", False),
            })
        return out
    except (OSError, UnicodeDecodeError, RuntimeError, ValueError):
        return []


def analyze_code(code: str, mcu: str = "") -> list[dict]:
    """分析代码字符串"""
    try:
        from embedguard.core.pipeline import AnalysisPipeline
        from embedguard.core.config import EmbedGuardConfig
    except ImportError:
        logging.warning("EmbedGuard 未安装，请先安装 embedguard 包")
        return []

    config = EmbedGuardConfig()
    if mcu:
        config.target_mcu = mcu

    pipeline = AnalysisPipeline(config)
    try:
        result = pipeline.analyze(code, target_mcu=mcu)
        findings = result.findings if hasattr(result, "findings") and result.findings else []
        out = []
        for f in findings:
            out.append({
                "rule": getattr(f, "rule_id", "UNKNOWN"),
                "severity": getattr(f, "severity", "warning"),
                "line": getattr(f, "line", 0),
                "message": getattr(f, "message", ""),
                "fixable": getattr(f, "fixable", False),
            })
        return out
    except (RuntimeError, ValueError, AttributeError):
        return []
