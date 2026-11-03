"""Antigravity Brain Artifacts discovery, task.md plan parsing, and Telegram card helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

from antigravity_telegram_bridge.i18n import tr as _t
from antigravity_telegram_bridge.paths import antigravity_home
from antigravity_telegram_bridge.telegram.notifications import split_markdown_pages

_RESOLVED_SUFFIX_RE = re.compile(r"\.resolved(?:\.\d+)?$", re.IGNORECASE)
_TASK_CHECKLIST_RE = re.compile(r"^\s*[-*]\s*\[([ xX/\-])\]\s+(.+?)\s*$")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp"}
_TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".toml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh", ".ps1",
    ".sql", ".xml", ".csv", ".ini", ".cfg", ".diff", ".patch",
}
_PRIORITY_NAMES = {
    "implementation_plan.md": 0,
    "walkthrough.md": 1,
    "task.md": 2,
}


@dataclass(frozen=True)
class ArtifactInfo:
    name: str
    path: Path
    size_bytes: int
    updated_at: float
    summary: str = ""
    request_feedback: bool = False
    user_facing: bool = True

    @property
    def is_image(self) -> bool:
        return self.path.suffix.lower() in _IMAGE_SUFFIXES

    @property
    def is_text(self) -> bool:
        suffix = self.path.suffix.lower()
        return suffix in _TEXT_SUFFIXES or suffix == ""


def _safe_brain_dir(thread_id: str, home: Path | None = None) -> Path | None:
    tid = str(thread_id or "").strip()
    if not tid or "/" in tid or "\\" in tid or ".." in tid:
        return None
    base_home = (home or antigravity_home()).resolve()
    brain_root = (base_home / "brain").resolve()
    target = (brain_root / tid).resolve()
    if not target.is_relative_to(brain_root):
        return None
    return target


def _parse_iso_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _read_artifact_metadata(file_path: Path, brain_dir: Path) -> dict[str, Any]:
    meta_path = Path(str(file_path) + ".metadata.json")
    try:
        if not meta_path.exists() or meta_path.is_symlink():
            return {}
        resolved = meta_path.resolve()
        if not resolved.is_relative_to(brain_dir):
            return {}
        raw = json.loads(resolved.read_text(encoding="utf-8", errors="replace"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def list_artifacts(thread_id: str, home: Path | None = None) -> list[ArtifactInfo]:
    """List user-facing artifacts in `<antigravity_home>/brain/<thread_id>/`."""
    brain_dir = _safe_brain_dir(thread_id, home=home)
    if brain_dir is None or not brain_dir.is_dir():
        return []

    results: list[ArtifactInfo] = []
    try:
        candidates = sorted(brain_dir.rglob("*"))
    except OSError:
        return []

    for candidate in candidates:
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(brain_dir):
                continue
            rel = resolved.relative_to(brain_dir)
            parts = rel.parts
            if not parts:
                continue
            # Skip hidden directories/files, .system_generated, and scratch
            if any(part.startswith(".") for part in parts):
                continue
            if parts[0].lower() in {".system_generated", "scratch"}:
                continue
            filename = parts[-1]
            if filename.endswith(".metadata.json"):
                continue
            if _RESOLVED_SUFFIX_RE.search(filename):
                continue

            stat = resolved.stat()
            meta = _read_artifact_metadata(resolved, brain_dir)
            user_facing = bool(meta.get("userFacing", True))
            if not user_facing:
                continue
            summary = str(meta.get("summary") or "").strip()
            request_feedback = bool(meta.get("requestFeedback", False))
            meta_ts = _parse_iso_timestamp(meta.get("updatedAt"))
            updated_at = meta_ts if meta_ts is not None else float(stat.st_mtime)
            rel_name = "/".join(parts)
            results.append(
                ArtifactInfo(
                    name=rel_name,
                    path=resolved,
                    size_bytes=int(stat.st_size),
                    updated_at=updated_at,
                    summary=summary,
                    request_feedback=request_feedback,
                    user_facing=user_facing,
                )
            )
        except OSError:
            continue

    results.sort(
        key=lambda item: (
            _PRIORITY_NAMES.get(item.name.lower(), 10),
            -item.updated_at,
            item.name.lower(),
        )
    )
    return results


def parse_task_plan(thread_id: str, home: Path | None = None) -> list[dict[str, str]]:
    """Parse markdown checklist items from `<antigravity_home>/brain/<thread_id>/task.md`."""
    brain_dir = _safe_brain_dir(thread_id, home=home)
    if brain_dir is None:
        return []
    task_path = brain_dir / "task.md"
    try:
        if not task_path.exists() or task_path.is_symlink() or not task_path.is_file():
            return []
        resolved = task_path.resolve()
        if not resolved.is_relative_to(brain_dir):
            return []
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    steps: list[dict[str, str]] = []
    for line in content.splitlines():
        match = _TASK_CHECKLIST_RE.match(line)
        if not match:
            continue
        marker, raw_text = match.group(1), match.group(2).strip()
        clean_text = _MD_LINK_RE.sub(r"\1", raw_text)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()
        if not clean_text:
            continue
        if marker in {"x", "X"}:
            status = "completed"
        elif marker == "/":
            status = "in_progress"
        else:
            status = "pending"
        steps.append({"step": clean_text, "status": status})
    return steps


def format_artifact_size(size_bytes: int) -> str:
    value = max(0, int(size_bytes))
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024.0:.1f} KB"
    return f"{value / (1024.0 * 1024.0):.2f} MB"


def format_artifacts_card(
    thread_id: str,
    artifacts: list[ArtifactInfo],
    *,
    title: str = "",
) -> tuple[str, dict[str, Any]]:
    """Format the Telegram overview card and inline keyboard for a thread's artifacts."""
    lines = [
        _t("📂 *Antigravity Artifacts*"),
        _t("- 会话 ID: `{thread_id}`", thread_id=thread_id),
    ]
    if title:
        lines.append(_t("- 标题: {title}", title=title))
    lines.append(_t("- 文件数量: `{count}`", count=len(artifacts)))

    rows: list[list[dict[str, str]]] = []
    if not artifacts:
        lines.extend(["", _t("当前会话暂无 Artifacts 产物文件。")])
    else:
        lines.append("")
        for idx, item in enumerate(artifacts[:12]):
            icon = "🖼" if item.is_image else "📄"
            feedback_badge = _t(" _(待确认)_") if item.request_feedback else ""
            lines.append(
                f"{idx + 1}. {icon} `{item.name}` ({format_artifact_size(item.size_bytes)}){feedback_badge}"
            )
            if item.summary:
                short_summary = re.sub(r"\s+", " ", item.summary).strip()
                if len(short_summary) > 140:
                    short_summary = short_summary[:137] + "..."
                lines.append(f"   _{short_summary}_")

            label_name = item.name if len(item.name) <= 22 else "..." + item.name[-19:]
            row: list[dict[str, str]] = []
            if item.is_text:
                row.append(
                    {
                        "text": f"📖 {idx + 1}. {label_name}",
                        "callback_data": f"artifactview|{thread_id}:{idx}:0",
                    }
                )
            row.append(
                {
                    "text": f"⬇️ {idx + 1}. {_t('下载')}",
                    "callback_data": f"artifactdl|{thread_id}:{idx}",
                }
            )
            rows.append(row)

    rows.append(
        [
            {"text": _t("🔄 刷新"), "callback_data": f"artifacts|{thread_id}"},
            {"text": _t("📋 返回任务"), "callback_data": f"task|{thread_id}"},
        ]
    )
    return "\n".join(lines), {"inline_keyboard": rows}


def format_artifact_view_card(
    thread_id: str,
    idx: int,
    artifacts: list[ArtifactInfo],
    page: int = 0,
) -> tuple[str, dict[str, Any], ArtifactInfo | None]:
    """Format a paginated preview card for a single text/markdown artifact."""
    if idx < 0 or idx >= len(artifacts):
        return (
            _t("未找到该 Artifact，可能已被清理。"),
            {"inline_keyboard": [[{"text": _t("📂 返回 Artifacts"), "callback_data": f"artifacts|{thread_id}"}]]},
            None,
        )

    item = artifacts[idx]
    if not item.is_text:
        text = "\n".join(
            [
                _t("🖼 *Artifact 文件*"),
                _t("- 名称: `{name}`", name=item.name),
                _t("- 大小: `{size}`", size=format_artifact_size(item.size_bytes)),
                *(
                    [_t("- 摘要: {summary}", summary=item.summary)]
                    if item.summary
                    else []
                ),
                "",
                _t("该文件为二进制或图片格式，请点击下方按钮下载查看。"),
            ]
        )
        markup = {
            "inline_keyboard": [
                [{"text": _t("⬇️ 下载文件"), "callback_data": f"artifactdl|{thread_id}:{idx}"}],
                [
                    {"text": _t("📂 返回 Artifacts"), "callback_data": f"artifacts|{thread_id}"},
                    {"text": _t("📋 返回任务"), "callback_data": f"task|{thread_id}"},
                ],
            ]
        }
        return text, markup, item

    try:
        raw_content = item.path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        raw_content = _t("读取文件失败: `{exc}`", exc=exc)

    if not raw_content:
        raw_content = _t("（文件内容为空）")

    pages = split_markdown_pages(raw_content, 2800)
    total_pages = max(1, len(pages))
    page = max(0, min(int(page), total_pages - 1))
    header = [
        _t("📄 *Artifact 预览*: `{name}` ({page}/{total})", name=item.name, page=page + 1, total=total_pages),
        _t("- 大小: `{size}`", size=format_artifact_size(item.size_bytes)),
    ]
    if item.request_feedback:
        header.append(_t("- 状态: ⏳ 等待用户确认 / 反馈"))
    if item.summary:
        short_summary = re.sub(r"\s+", " ", item.summary).strip()
        if len(short_summary) > 180:
            short_summary = short_summary[:177] + "..."
        header.append(_t("- 摘要: {summary}", summary=short_summary))
    header.append("")

    body = pages[page]
    text = "\n".join(header) + body

    rows: list[list[dict[str, str]]] = []
    nav_row: list[dict[str, str]] = []
    if page > 0:
        nav_row.append(
            {
                "text": _t("⬅️ 上一页"),
                "callback_data": f"artifactview|{thread_id}:{idx}:{page - 1}",
            }
        )
    if page + 1 < total_pages:
        nav_row.append(
            {
                "text": _t("下一页 ➡️"),
                "callback_data": f"artifactview|{thread_id}:{idx}:{page + 1}",
            }
        )
    if nav_row:
        rows.append(nav_row)

    rows.append(
        [
            {"text": _t("⬇️ 下载原文件"), "callback_data": f"artifactdl|{thread_id}:{idx}"},
            {"text": _t("📂 Artifacts 列表"), "callback_data": f"artifacts|{thread_id}"},
        ]
    )
    rows.append(
        [
            {"text": _t("📋 返回任务"), "callback_data": f"task|{thread_id}"},
        ]
    )
    return text, {"inline_keyboard": rows}, item
