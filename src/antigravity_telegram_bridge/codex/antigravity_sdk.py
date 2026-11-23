"""Official google-antigravity Python SDK runtime backend for Telegram-managed sessions.

Provides a resilient, documented execution path (pip install google-antigravity)
that does not depend on local Desktop LS port discovery or Hook env propagation.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
import importlib.util
import inspect
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable
import uuid

from ..i18n import tr as _t
from ..paths import ensure_data_dir

SDK_THREAD_PREFIX = "agy-sdk-"
MAX_PERSISTED_THREADS = 100
MAX_PERSISTED_TURNS_PER_THREAD = 30


def sdk_available() -> bool:
    """Return True when the official google-antigravity Python SDK is importable."""
    try:
        return importlib.util.find_spec("google.antigravity") is not None
    except (ModuleNotFoundError, ValueError, ImportError):
        return False


def is_sdk_thread_id(thread_id: str) -> bool:
    return isinstance(thread_id, str) and thread_id.startswith(SDK_THREAD_PREFIX)


def _import_sdk():
    from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig

    return Agent, LocalAgentConfig, CapabilitiesConfig


def _build_read_only_capabilities(capabilities_cls: Any) -> Any:
    """Build strict read-only CapabilitiesConfig when supported by the installed SDK."""
    try:
        sig = inspect.signature(capabilities_cls)
        cap_params = set(sig.parameters.keys())
    except (TypeError, ValueError):
        return None
    if "enabled_tools" not in cap_params and "enable_subagents" not in cap_params:
        return None
    kwargs: dict[str, Any] = {}
    if "enable_subagents" in cap_params:
        kwargs["enable_subagents"] = False
    if "enabled_tools" in cap_params:
        try:
            from google.antigravity import BuiltinTools

            ro_tools = [
                getattr(BuiltinTools, attr)
                for attr in ("LIST_DIR", "SEARCH_DIR", "FIND_FILE", "VIEW_FILE")
                if hasattr(BuiltinTools, attr)
            ]
            if ro_tools:
                kwargs["enabled_tools"] = ro_tools
        except Exception:
            pass
    try:
        return capabilities_cls(**kwargs)
    except Exception:
        return None


def _build_local_agent_config(
    local_config_cls: Any,
    capabilities_cls: Any,
    *,
    system_instructions: str,
    cwd: str = "",
    model: str = "",
    read_only: bool = False,
) -> Any:
    """Construct LocalAgentConfig defensively across SDK versions."""
    instructions = system_instructions.strip()
    if cwd:
        workspace_note = f"Primary working directory: {cwd}"
        instructions = f"{instructions}\n\n{workspace_note}" if instructions else workspace_note

    kwargs: dict[str, Any] = {}
    try:
        sig = inspect.signature(local_config_cls)
        params = set(sig.parameters.keys())
        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except (TypeError, ValueError):
        params = {"system_instructions", "capabilities"}
        accepts_var_kw = False

    def _maybe_set(key: str, value: Any) -> None:
        if value is not None and (accepts_var_kw or key in params):
            kwargs[key] = value

    _maybe_set("system_instructions", instructions)
    if read_only:
        ro_cap = _build_read_only_capabilities(capabilities_cls)
        if ro_cap is not None:
            _maybe_set("capabilities", ro_cap)
    else:
        _maybe_set("capabilities", capabilities_cls())
    if cwd:
        if "workspaces" in params:
            kwargs["workspaces"] = [cwd]
        else:
            for cwd_key in ("workspace_path", "working_directory", "cwd"):
                if cwd_key in params:
                    kwargs[cwd_key] = cwd
                    break
    if model and model.strip() not in {"pro", "flash", "flash_lite"}:
        for model_key in ("model", "model_name"):
            if model_key in params:
                kwargs[model_key] = model.strip()
                break
    return local_config_cls(**kwargs)


def _summarize_prior_turns(turns: list[dict[str, Any]], max_turns: int = 6) -> str:
    """Build concise context for re-hydrating an SDK agent after a bot restart."""
    lines: list[str] = []
    for turn in turns[-max_turns:]:
        if not isinstance(turn, dict):
            continue
        for item in turn.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "userMessage":
                text = "\n".join(
                    str(c.get("text") or "")
                    for c in (item.get("content") or [])
                    if isinstance(c, dict) and c.get("type") == "text"
                ).strip()
                if text:
                    lines.append(f"User: {text[:1200]}")
            elif item_type == "agentMessage" and item.get("text"):
                lines.append(f"Assistant: {str(item['text'])[:1500]}")
    if not lines:
        return ""
    return (
        "Previous conversation context (restored after bridge restart):\n"
        + "\n\n".join(lines)
    )


class AntigravitySdkRuntime:
    """Thread-safe lifecycle manager for official google-antigravity SDK sessions."""

    def __init__(
        self,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        *,
        data: Path | None = None,
        sdk_loader: Callable[[], tuple[Any, Any, Any]] | None = None,
    ) -> None:
        self.on_event = on_event
        self._data_dir = data
        self._sdk_loader = sdk_loader or _import_sdk
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._closed = False
        self._threads: dict[str, dict[str, Any]] = {}
        self._agents: dict[str, Any] = {}
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._active_responses: dict[str, Any] = {}
        self._load_persisted()

    def _store_path(self) -> Path:
        base = self._data_dir if self._data_dir is not None else ensure_data_dir()
        return base / "antigravity_sdk_threads.json"

    def _load_persisted(self) -> None:
        path = self._store_path()
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for tid, entry in raw.items():
            if not isinstance(tid, str) or not is_sdk_thread_id(tid) or not isinstance(entry, dict):
                continue
            normalized = dict(entry)
            # Any turn marked inProgress before a crash/restart is no longer active.
            normalized["status"] = {"type": "idle", "activeFlags": []}
            turns = normalized.get("turns")
            if isinstance(turns, list):
                for turn in turns:
                    if isinstance(turn, dict) and turn.get("status") == "inProgress":
                        turn["status"] = "interrupted"
            self._threads[tid] = normalized

    def _persist_locked(self) -> None:
        ordered = sorted(
            self._threads.items(),
            key=lambda kv: float(kv[1].get("updatedAt") or 0),
            reverse=True,
        )[:MAX_PERSISTED_THREADS]
        snapshot: dict[str, Any] = {}
        for tid, entry in ordered:
            item = dict(entry)
            turns = item.get("turns")
            if isinstance(turns, list) and len(turns) > MAX_PERSISTED_TURNS_PER_THREAD:
                item["turns"] = turns[-MAX_PERSISTED_TURNS_PER_THREAD:]
            snapshot[tid] = item
        path = self._store_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".agy-sdk-",
                delete=False,
            ) as handle:
                tmp = Path(handle.name)
                json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
        except OSError:
            pass
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._closed:
                raise RuntimeError(_t("Antigravity SDK 运行时已停止。"))
            if self._loop is not None and self._loop.is_running():
                return self._loop
            self._loop_ready.clear()

            def _runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                self._loop_ready.set()
                try:
                    loop.run_forever()
                finally:
                    try:
                        loop.close()
                    except Exception:
                        pass

            self._loop_thread = threading.Thread(
                target=_runner,
                name="antigravity-sdk-loop",
                daemon=True,
            )
            self._loop_thread.start()
        self._loop_ready.wait(timeout=5.0)
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError(_t("无法启动 Antigravity SDK 事件循环。"))
        return self._loop

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:
            pass

    def owns_thread(self, thread_id: str) -> bool:
        with self._lock:
            return is_sdk_thread_id(thread_id) and thread_id in self._threads

    def list_threads(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            items = [
                {k: v for k, v in entry.items() if k != "turns"}
                for entry in self._threads.values()
            ]
        items.sort(key=lambda item: item.get("updatedAt") or 0, reverse=True)
        return items[:limit]

    def read_thread(self, thread_id: str, *, include_turns: bool = True) -> dict[str, Any] | None:
        with self._lock:
            entry = self._threads.get(thread_id)
            if entry is None:
                return None
            copy = json.loads(json.dumps(entry))
        if not include_turns:
            copy.pop("turns", None)
        return copy

    def create_thread(
        self,
        *,
        cwd: str = "",
        model: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        tid = f"{SDK_THREAD_PREFIX}{uuid.uuid4().hex[:12]}"
        now = int(time.time())
        clean_title = (title or "").strip()[:80] or tid[:16]
        entry: dict[str, Any] = {
            "id": tid,
            "name": clean_title,
            "title": clean_title,
            "preview": clean_title,
            "cwd": cwd or str(Path.home()),
            "model": model or "pro",
            "status": {"type": "idle", "activeFlags": []},
            "updatedAt": now,
            "stepCount": 0,
            "source": "antigravity-sdk",
            "agentName": "antigravity-sdk",
            "parentConversationId": "",
            "nestingDepth": 0,
            "isSubagent": False,
            "killed": False,
            "turns": [],
        }
        with self._lock:
            self._threads[tid] = entry
            self._persist_locked()
            return json.loads(json.dumps(entry))

    def start_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        if not prompt or not prompt.strip():
            raise ValueError(_t("回复内容不能为空。"))
        if not self._sdk_loader:
            raise RuntimeError(_t("未安装 google-antigravity SDK。"))
        loop = self._ensure_loop()
        with self._lock:
            entry = self._threads.get(thread_id)
            if entry is None:
                if not is_sdk_thread_id(thread_id):
                    raise ValueError(_t("目标会话不是由 Antigravity SDK 托管的任务。"))
                entry = {
                    "id": thread_id,
                    "name": prompt.strip().splitlines()[0][:80],
                    "title": prompt.strip().splitlines()[0][:80],
                    "preview": prompt.strip()[:160],
                    "cwd": cwd or str(Path.home()),
                    "model": model or "pro",
                    "status": {"type": "idle", "activeFlags": []},
                    "updatedAt": int(time.time()),
                    "stepCount": 0,
                    "source": "antigravity-sdk",
                    "agentName": "antigravity-sdk",
                    "parentConversationId": "",
                    "nestingDepth": 0,
                    "isSubagent": False,
                    "killed": False,
                    "turns": [],
                }
                self._threads[thread_id] = entry
            if (entry.get("status") or {}).get("type") == "active":
                raise RuntimeError(_t("该任务当前仍有运行中的 Turn。"))

            turns = entry.setdefault("turns", [])
            turn_index = len(turns)
            turn_id = f"turn-{turn_index}-{uuid.uuid4().hex[:6]}"
            now_ts = time.time()
            first_line = prompt.strip().splitlines()[0][:80]
            if not turns:
                entry["name"] = first_line
                entry["title"] = first_line
                entry["preview"] = prompt.strip()[:160]
            if cwd:
                entry["cwd"] = cwd
            if model:
                entry["model"] = model
            turn_record: dict[str, Any] = {
                "id": turn_id,
                "status": "inProgress",
                "startedAt": now_ts,
                "durationMs": 0,
                "items": [
                    {
                        "id": f"item-user-{turn_index}",
                        "type": "userMessage",
                        "content": [{"type": "text", "text": prompt}],
                    }
                ],
            }
            turns.append(turn_record)
            entry["status"] = {"type": "active", "activeFlags": []}
            entry["updatedAt"] = int(now_ts)
            entry["stepCount"] = int(entry.get("stepCount") or 0) + 1
            prior_context = _summarize_prior_turns(turns[:-1]) if thread_id not in self._agents else ""
            effective_cwd = str(entry.get("cwd") or "")
            effective_model = str(entry.get("model") or "")
            self._persist_locked()

        asyncio.run_coroutine_threadsafe(
            self._launch_turn(
                thread_id=thread_id,
                turn_id=turn_id,
                prompt=prompt,
                cwd=effective_cwd,
                model=effective_model,
                prior_context=prior_context,
            ),
            loop,
        )
        return {"turn": {"id": turn_id, "status": "inProgress"}, "threadId": thread_id}

    async def _get_or_create_agent(
        self,
        thread_id: str,
        *,
        cwd: str,
        model: str,
        prior_context: str,
    ) -> Any:
        agent = self._agents.get(thread_id)
        if agent is not None:
            return agent
        agent_cls, local_config_cls, capabilities_cls = self._sdk_loader()
        instructions = "You are Google Antigravity assisting the user remotely via Telegram."
        if prior_context:
            instructions = f"{instructions}\n\n{prior_context}"
        config = _build_local_agent_config(
            local_config_cls,
            capabilities_cls,
            system_instructions=instructions,
            cwd=cwd,
            model=model,
            read_only=False,
        )
        agent = agent_cls(config)
        entered = await agent.__aenter__()
        live_agent = entered if entered is not None else agent
        self._agents[thread_id] = (agent, live_agent)
        return (agent, live_agent)

    async def _launch_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        prompt: str,
        cwd: str,
        model: str,
        prior_context: str,
    ) -> None:
        task = asyncio.create_task(
            self._run_turn_coro(
                thread_id=thread_id,
                turn_id=turn_id,
                prompt=prompt,
                cwd=cwd,
                model=model,
                prior_context=prior_context,
            )
        )
        with self._lock:
            self._active_tasks[thread_id] = task
        try:
            await task
        finally:
            with self._lock:
                if self._active_tasks.get(thread_id) is task:
                    self._active_tasks.pop(thread_id, None)

    async def _run_turn_coro(
        self,
        *,
        thread_id: str,
        turn_id: str,
        prompt: str,
        cwd: str,
        model: str,
        prior_context: str,
    ) -> None:
        started_at = time.time()
        self._emit(
            {
                "method": "turn/started",
                "params": {"threadId": thread_id, "turnId": turn_id, "turn": {"id": turn_id}},
            }
        )
        tool_items: list[dict[str, Any]] = []
        chunks: list[str] = []
        status = "completed"
        error_message = ""
        try:
            _cm, agent = await self._get_or_create_agent(
                thread_id,
                cwd=cwd,
                model=model,
                prior_context=prior_context,
            )
            response = await agent.chat(prompt)
            with self._lock:
                self._active_responses[thread_id] = response

            async def _drain_tools() -> None:
                tool_stream = getattr(response, "tool_calls", None)
                if tool_stream is None or not hasattr(tool_stream, "__aiter__"):
                    return
                idx = 0
                async for call in tool_stream:
                    idx += 1
                    name = str(getattr(call, "name", "") or "tool")
                    args = getattr(call, "args", None)
                    summary = name
                    if isinstance(args, dict):
                        cmd_arg = args.get("CommandLine") or args.get("Path") or args.get("query") or ""
                        if cmd_arg:
                            summary = f"{name} {cmd_arg}"
                    item = {
                        "id": f"tool-{turn_id}-{idx}",
                        "type": "commandExecution",
                        "command": summary[:240],
                    }
                    tool_items.append(item)
                    self._emit(
                        {
                            "method": "item/started",
                            "params": {"threadId": thread_id, "turnId": turn_id, "item": item},
                        }
                    )
                    self._emit(
                        {
                            "method": "item/completed",
                            "params": {"threadId": thread_id, "turnId": turn_id, "item": item},
                        }
                    )

            async def _drain_text() -> None:
                async for token in response:
                    if token:
                        chunks.append(str(token))

            tool_task = asyncio.create_task(_drain_tools())
            try:
                await _drain_text()
            finally:
                if not tool_task.done():
                    try:
                        await asyncio.wait_for(tool_task, timeout=1.0)
                    except Exception:
                        tool_task.cancel()
        except asyncio.CancelledError:
            status = "interrupted"
        except Exception as exc:
            status = "failed"
            error_message = str(exc)
        finally:
            with self._lock:
                self._active_responses.pop(thread_id, None)

        final_text = "".join(chunks).strip()
        duration_ms = int(max(0.0, time.time() - started_at) * 1000)
        final_items: list[dict[str, Any]] = list(tool_items)
        if final_text:
            agent_item = {
                "id": f"agent-{turn_id}",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": final_text,
            }
            final_items.append(agent_item)
            self._emit(
                {
                    "method": "item/completed",
                    "params": {"threadId": thread_id, "turnId": turn_id, "item": agent_item},
                }
            )

        with self._lock:
            entry = self._threads.get(thread_id)
            if isinstance(entry, dict):
                entry["status"] = {"type": "idle", "activeFlags": []}
                entry["updatedAt"] = int(time.time())
                for turn in reversed(entry.get("turns") or []):
                    if isinstance(turn, dict) and turn.get("id") == turn_id:
                        turn["status"] = status
                        turn["durationMs"] = duration_ms
                        turn.setdefault("items", []).extend(final_items)
                        break
                entry["stepCount"] = int(entry.get("stepCount") or 0) + max(1, len(final_items))
                self._persist_locked()

        turn_payload: dict[str, Any] = {
            "id": turn_id,
            "status": status,
            "items": final_items,
            "durationMs": duration_ms,
        }
        if error_message:
            turn_payload["error"] = {"message": error_message}
        self._emit(
            {
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turnId": turn_id, "turn": turn_payload},
            }
        )

    def interrupt_turn(self, thread_id: str) -> bool:
        with self._lock:
            task = self._active_tasks.get(thread_id)
            response = self._active_responses.get(thread_id)
            loop = self._loop
        if task is None or loop is None or not loop.is_running():
            return False

        async def _cancel() -> None:
            if response is not None and callable(getattr(response, "cancel", None)):
                try:
                    await response.cancel()
                except Exception:
                    pass
            task.cancel()

        asyncio.run_coroutine_threadsafe(_cancel(), loop)
        return True

    def side_chat_ask(
        self,
        *,
        system_instructions: str,
        prompt: str,
        cwd: str = "",
        timeout: float = 300.0,
    ) -> str:
        """Execute a strictly read-only side question using the official SDK."""
        loop = self._ensure_loop()

        async def _ask() -> str:
            agent_cls, local_config_cls, capabilities_cls = self._sdk_loader()
            config = _build_local_agent_config(
                local_config_cls,
                capabilities_cls,
                system_instructions=system_instructions,
                cwd=cwd,
                read_only=True,
            )
            async with agent_cls(config) as agent:
                response = await agent.chat(prompt)
                parts: list[str] = []
                async for token in response:
                    if token:
                        parts.append(str(token))
                return "".join(parts).strip()

        fut: Future[str] = asyncio.run_coroutine_threadsafe(_ask(), loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        with self._lock:
            self._closed = True
            loop = self._loop
            thread = self._loop_thread
            tasks = list(self._active_tasks.values())
            agents = list(self._agents.values())
            self._active_tasks.clear()
            self._agents.clear()
        if loop is not None and loop.is_running():
            async def _shutdown() -> None:
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                for cm, _live in agents:
                    try:
                        await cm.__aexit__(None, None, None)
                    except Exception:
                        pass

            try:
                asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(timeout=5.0)
            except Exception:
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
