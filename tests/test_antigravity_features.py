"""Offline unit tests for Antigravity store, Brain artifacts, hooks, and Telegram bot integration."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from antigravity_telegram_bridge import app as b
from antigravity_telegram_bridge.codex import antigravity_store as agy_store
from antigravity_telegram_bridge.codex import receipts
from antigravity_telegram_bridge.features import artifacts as agy_artifacts
from antigravity_telegram_bridge.management import desktop_hook


def _create_antigravity_fixture(home: Path, thread_id: str = "0347a2a0-9035-401e-bda2-98081f8c8d9c") -> Path:
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "conversation_summaries.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, "
            "title TEXT, "
            "preview TEXT, "
            "workspace_uris TEXT, "
            "status TEXT, "
            "last_modified_time TEXT, "
            "step_count INTEGER, "
            "source TEXT, "
            "agent_name TEXT, "
            "parent_conversation_id TEXT, "
            "nesting_depth INTEGER, "
            "killed INTEGER, "
            "not_fully_idle INTEGER, "
            "app_data_dir TEXT)"
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                "Port Bridge to Antigravity",
                "Migrate the bridge to Antigravity",
                json.dumps([(home / "workspace").resolve().as_uri()]),
                "CASCADE_RUN_STATUS_IDLE",
                "2026-11-03T10:05:00+08:00",
                2,
                "antigravity",
                "",
                "",
                0,
                0,
                0,
                str(home),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    brain_dir = home / "brain" / thread_id
    logs_dir = brain_dir / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    compact_steps = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-03T10:00:01+08:00",
            "content": "<USER_REQUEST>Migrate the bridge to Antigravity</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-03T10:01:00+08:00",
            "content": "truncated",
            "truncated_fields": ["content"],
            "input_tokens": 1200,
            "cache_read_tokens": 800,
            "output_tokens": 500,
            "tool_calls": [
                {
                    "id": "call-edit-1",
                    "name": "edit_file",
                    "arguments": {
                        "Path": str(home / "workspace" / "paths.py"),
                        "Target": "OLD_NAME = 'codex'\n",
                        "Replacement": "NEW_NAME = 'antigravity'\n",
                    },
                },
                {
                    "id": "call-cmd-1",
                    "name": "run_command",
                    "arguments": {
                        "CommandLine": "pytest -q",
                        "toolSummary": "Run unit tests",
                    },
                },
                {
                    "id": "call-ask-1",
                    "name": "ask_question",
                    "arguments": {
                        "questions": [
                            {
                                "question": "Which quota mode should we use?",
                                "options": ["(Recommended) Model & Token stats", "Disable quota"],
                            }
                        ]
                    },
                },
            ],
        },
    ]
    full_steps = [
        compact_steps[0],
        {
            **compact_steps[1],
            "content": "Full planner response from transcript_full.jsonl",
            "truncated_fields": [],
        },
    ]
    (logs_dir / "transcript.jsonl").write_text(
        "\n".join(json.dumps(step) for step in compact_steps) + "\n",
        encoding="utf-8",
    )
    (logs_dir / "transcript_full.jsonl").write_text(
        "\n".join(json.dumps(step) for step in full_steps) + "\n",
        encoding="utf-8",
    )

    # Create artifacts in brain/<thread_id>
    (brain_dir / "implementation_plan.md").write_text(
        "# Implementation Plan\n\nDetailed migration steps.\n",
        encoding="utf-8",
    )
    (brain_dir / "implementation_plan.md.metadata.json").write_text(
        json.dumps(
            {
                "summary": "Core migration plan for Antigravity",
                "userFacing": True,
                "requestFeedback": True,
                "updatedAt": "2026-11-03T10:02:00Z",
            }
        ),
        encoding="utf-8",
    )
    (brain_dir / "implementation_plan.md.resolved.0").write_text("old snapshot", encoding="utf-8")
    (brain_dir / "task.md").write_text(
        "# Tasks\n- [x] Update [paths.py](file:///d:/paths.py)\n- [/] Implement artifacts support\n- [ ] Live bot testing\n",
        encoding="utf-8",
    )
    (brain_dir / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-image")
    (brain_dir / "hidden.txt").write_text("internal", encoding="utf-8")
    (brain_dir / "hidden.txt.metadata.json").write_text(
        json.dumps({"userFacing": False}),
        encoding="utf-8",
    )
    scratch_dir = brain_dir / "scratch"
    scratch_dir.mkdir(exist_ok=True)
    (scratch_dir / "temp.py").write_text("print(1)", encoding="utf-8")
    return brain_dir


class AntigravityStoreAndArtifactsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "antigravity"
        self.data = Path(tmp.name) / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.thread_id = "0347a2a0-9035-401e-bda2-98081f8c8d9c"
        _create_antigravity_fixture(self.home, self.thread_id)

        p1 = patch("antigravity_telegram_bridge.paths.codex_home", return_value=self.home)
        p2 = patch("antigravity_telegram_bridge.paths.antigravity_home", return_value=self.home)
        p3 = patch("antigravity_telegram_bridge.paths.data_dir", return_value=self.data)
        p3b = patch("antigravity_telegram_bridge.storage.state.data_dir", return_value=self.data)
        p4 = patch.object(agy_store, "antigravity_home", return_value=self.home)
        p5 = patch.object(agy_store, "ensure_data_dir", return_value=self.data)
        p6 = patch.object(agy_artifacts, "antigravity_home", return_value=self.home)
        p7 = patch.object(desktop_hook, "codex_home", return_value=self.home)
        p8 = patch.object(desktop_hook, "ensure_data_dir", return_value=self.data)
        p9 = patch.object(desktop_hook, "hooks_json_path", return_value=self.home / "agy-hooks.json")
        for p in (p1, p2, p3, p3b, p4, p5, p6, p7, p8, p9):
            p.start()
            self.addCleanup(p.stop)

    def test_store_reads_summaries_transcript_full_diff_and_questions(self):
        self.assertTrue(agy_store.has_antigravity_data(self.home))
        summary = agy_store.read_conversation_summary(self.thread_id, self.home)
        self.assertIsNotNone(summary)
        self.assertEqual(summary["title"], "Port Bridge to Antigravity")

        threads = agy_store.list_antigravity_threads(self.home, limit=10)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["id"], self.thread_id)

        thread = agy_store.build_thread_from_antigravity(
            self.thread_id, home=self.home, include_turns=True
        )
        self.assertIsNotNone(thread)
        self.assertEqual(thread["title"], "Port Bridge to Antigravity")
        self.assertEqual(len(thread["turns"]), 1)
        turn = thread["turns"][0]
        self.assertEqual(turn["id"], "turn-0")
        items = turn["items"]
        user_items = [item for item in items if item["type"] == "userMessage"]
        agent_items = [item for item in items if item["type"] == "agentMessage"]
        diff_items = [item for item in items if item["type"] == "fileChange"]
        cmd_items = [item for item in items if item["type"] == "commandExecution"]

        self.assertEqual(user_items[0]["id"], "item-0")
        self.assertEqual(len(agent_items[0]["questions"]), 1)
        self.assertEqual(
            agent_items[0]["questions"][0]["title"],
            "Which quota mode should we use?",
        )
        # Verifies truncated_fields fell back to transcript_full.jsonl
        self.assertEqual(agent_items[-1]["text"], "Full planner response from transcript_full.jsonl")
        self.assertIn("-OLD_NAME = 'codex'", diff_items[0]["changes"][0]["diff"])
        self.assertIn("+NEW_NAME = 'antigravity'", diff_items[0]["changes"][0]["diff"])
        self.assertEqual(cmd_items[0]["command"], "pytest -q")

    def test_token_usage_and_quota_stats_aggregation(self):
        agy_store.save_runtime_env(
            {"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:50051"},
            {"modelName": "claude-opus-4-6-thinking", "conversationId": self.thread_id},
            data=self.data,
        )
        usage = agy_store.compute_antigravity_token_usage(self.thread_id, self.home)
        self.assertIsNotNone(usage)
        self.assertEqual(usage["total"]["inputTokens"], 1200)
        self.assertEqual(usage["total"]["cachedInputTokens"], 800)
        self.assertEqual(usage["total"]["outputTokens"], 500)
        self.assertEqual(usage["total"]["totalTokens"], 2500)

        stats = agy_store.aggregate_antigravity_quota_stats(
            home=self.home, data=self.data, limit=10
        )["antigravityUsage"]
        self.assertEqual(stats["currentModel"], "claude-opus-4-6-thinking")
        self.assertTrue(stats["lsConnected"])
        self.assertEqual(stats["totalTokens"], 2500)
        self.assertEqual(len(stats["recentTasks"]), 1)

    def test_pending_control_queue_and_post_invocation_hook(self):
        agy_store.queue_pending_control(
            self.thread_id,
            inject_message="Follow up instruction",
            data=self.data,
        )
        stdin_payload = json.dumps({"conversationId": self.thread_id, "executionNum": 1})
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin_payload)), patch("sys.stdout", out):
            rc = desktop_hook.main([])
        self.assertEqual(rc, 0)
        response = json.loads(out.getvalue().strip().splitlines()[0])
        self.assertIn("additionalContext", response)
        self.assertIn("Follow up instruction", response["additionalContext"])
        self.assertEqual(response["injectSteps"], [{"userMessage": "Follow up instruction"}])
        self.assertEqual(response["terminationBehavior"], "force_continue")
        # Queue is consumed once
        self.assertEqual(agy_store.consume_pending_control(self.thread_id, data=self.data), {})

        # Test terminate control
        agy_store.queue_pending_control(self.thread_id, stop=True, data=self.data)
        out_term = io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin_payload)), patch("sys.stdout", out_term):
            desktop_hook.main([])
        term_response = json.loads(out_term.getvalue().strip().splitlines()[0])
        self.assertEqual(term_response["terminationBehavior"], "terminate")

    def test_registration_plan_registers_and_removes_antigravity_hooks(self):
        agy_hooks_file = self.home / "agy-hooks.json"
        agy_hooks_file.write_text(json.dumps({"user-plugin": {"enabled": True}}), encoding="utf-8")
        _cmd, changes = desktop_hook.registration_plan(remove=False)
        self.assertIn(agy_hooks_file, changes)
        registered = json.loads(changes[agy_hooks_file].decode("utf-8"))
        self.assertIn("user-plugin", registered)
        self.assertIn("antigravity-telegram-bridge", registered)
        bridge_hooks = registered["antigravity-telegram-bridge"]
        for event in ("SessionStart", "PreToolUse", "PostInvocation", "Stop"):
            self.assertIn(event, bridge_hooks)
        self.assertEqual(bridge_hooks["SessionStart"][0]["type"], "command")
        self.assertEqual(bridge_hooks["PreToolUse"][0]["matcher"], "ask_question")
        self.assertEqual(bridge_hooks["PreToolUse"][0]["hooks"][0]["type"], "command")
        self.assertEqual(bridge_hooks["PostInvocation"][0]["type"], "command")
        self.assertEqual(bridge_hooks["Stop"][0]["type"], "command")

        agy_hooks_file.write_bytes(changes[agy_hooks_file])
        _cmd, remove_changes = desktop_hook.registration_plan(remove=True)
        cleaned = json.loads(remove_changes[agy_hooks_file].decode("utf-8"))
        self.assertIn("user-plugin", cleaned)
        self.assertNotIn("antigravity-telegram-bridge", cleaned)

    def test_receipts_read_antigravity_transcript(self):
        sessions_dir = self.home / "sessions"
        path = receipts.session_path(self.thread_id, sessions_dir)
        self.assertIsNotNone(path)
        records, new_offset = receipts.read_receipts(path, 0)
        self.assertGreater(new_offset, 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["turn_id"], "turn-0")
        self.assertEqual(records[0]["item_id"], "item-0")
        self.assertEqual(
            records[0]["digest"],
            receipts.prompt_digest("", "Migrate the bridge to Antigravity"),
        )
        self.assertEqual(
            receipts.completion_turn(
                self.thread_id,
                "Full planner response from transcript_full.jsonl",
                sessions_dir,
            ),
            "turn-0",
        )

    def test_artifacts_listing_plan_parsing_and_cards(self):
        items = agy_artifacts.list_artifacts(self.thread_id, home=self.home)
        names = [item.name for item in items]
        # Priority order: implementation_plan.md, task.md, preview.png
        # Filtered out: .system_generated, scratch/temp.py, *.metadata.json, *.resolved.0, hidden.txt
        self.assertEqual(names, ["implementation_plan.md", "task.md", "preview.png"])
        self.assertTrue(items[0].request_feedback)
        self.assertEqual(items[0].summary, "Core migration plan for Antigravity")
        self.assertTrue(items[2].is_image)

        plan = agy_artifacts.parse_task_plan(self.thread_id, home=self.home)
        self.assertEqual(
            plan,
            [
                {"step": "Update paths.py", "status": "completed"},
                {"step": "Implement artifacts support", "status": "in_progress"},
                {"step": "Live bot testing", "status": "pending"},
            ],
        )

        card_text, markup = agy_artifacts.format_artifacts_card(
            self.thread_id, items, title="Port Bridge to Antigravity"
        )
        self.assertIn("implementation_plan.md", card_text)
        self.assertIn("Core migration plan for Antigravity", card_text)
        callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
        self.assertIn(f"artifactview|{self.thread_id}:0:0", callbacks)
        self.assertIn(f"artifactdl|{self.thread_id}:2", callbacks)

        view_text, view_markup, item = agy_artifacts.format_artifact_view_card(
            self.thread_id, 0, items, page=0
        )
        self.assertIsNotNone(item)
        self.assertIn("# Implementation Plan", view_text)

    def test_bot_quota_artifacts_and_task_detail_integration(self):
        agy_store.save_runtime_env(
            {"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:50051", "ANTIGRAVITY_CSRF_TOKEN": "csrf-123"},
            {"modelName": "claude-opus-4-6-thinking", "conversationId": self.thread_id},
            data=self.data,
        )
        fake_quota_summary = {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "gemini-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.92,
                                "resetTime": "2026-11-09T03:49:52Z",
                            },
                            {
                                "bucketId": "gemini-5h",
                                "window": "5h",
                                "remainingFraction": 0.91,
                                "resetTime": "2026-11-03T10:28:26Z",
                            },
                        ],
                    },
                    {
                        "displayName": "Claude and GPT models",
                        "buckets": [
                            {
                                "bucketId": "3p-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.268,
                                "resetTime": "2026-11-09T10:40:12Z",
                            },
                            {
                                "bucketId": "3p-5h",
                                "window": "5h",
                                "remainingFraction": 1.0,
                                "resetTime": "2026-11-03T12:25:41Z",
                            },
                        ],
                    },
                ]
            }
        }
        with patch.object(b, "STATE_PATH", self.data / "telegram_bot.state.json"), \
             patch.object(b, "codex_home", return_value=self.home), \
             patch.object(agy_store, "_call_ls_rpc", return_value=fake_quota_summary), \
             patch.object(b, "send_document", return_value=True) as upload_doc:
            bot = b.TelegramCodexBot("111:fake", {"chat_id": "123"})
            bot.send = Mock(return_value={"message_id": 42})
            bot.edit_message = Mock(return_value={"message_id": 42})
            bot._reaction = Mock(return_value=True)
            bot._operation_reaction = Mock(return_value=True)
            bot._answer_callback = Mock()
            # 1. /quota renders Antigravity remaining quota card (Gemini + Claude/GPT 5h & weekly)
            bot.command_quota()
            quota_text = bot.edit_message.call_args.args[1]
            self.assertIn("**📊 Antigravity 剩余额度**", quota_text)
            self.assertIn("**🤖 Gemini 模型**", quota_text)
            self.assertIn("**🧠 Claude / GPT 模型**", quota_text)
            self.assertIn("**5 小时窗口：** 剩余约 91%", quota_text)
            self.assertIn("**7 天窗口：** 剩余约 92%", quota_text)
            self.assertIn("**5 小时窗口：** 剩余约 100%", quota_text)
            self.assertIn("**7 天窗口：** 剩余约 26.8%", quota_text)
            self.assertIn("**重置时间：**", quota_text)
            self.assertIn("**距离重置：**", quota_text)

            # 2. show_task includes Artifacts count and button
            bot.send.reset_mock()
            bot.show_task(self.thread_id)
            task_text, task_markup = bot.send.call_args.args[0], bot.send.call_args.args[1]
            self.assertIn("Artifacts", task_text)
            task_callbacks = [
                btn["callback_data"] for row in task_markup["inline_keyboard"] for btn in row
            ]
            self.assertIn(f"artifacts|{self.thread_id}", task_callbacks)

            # 3. /artifacts command & preview/download callbacks
            bot.send.reset_mock()
            bot.command_artifacts(self.thread_id)
            art_text = bot.send.call_args.args[0]
            self.assertIn("implementation_plan.md", art_text)

            bot.send.reset_mock()
            bot.show_artifact_view(f"{self.thread_id}:0:0")
            preview_text = bot.send.call_args.args[0]
            self.assertIn("Implementation Plan", preview_text)

            bot.send_artifact_file(f"{self.thread_id}:0")
            upload_doc.assert_called_once()

            # 4. /usage renders full token breakdown & elapsed time from Antigravity transcript
            bot.send.reset_mock()
            bot.command_usage(self.thread_id)
            usage_text = bot.send.call_args.args[0]
            self.assertIn("当前线程累计", usage_text)
            self.assertIn("2,500", usage_text)
            self.assertIn("1,200", usage_text)
            self.assertIn("800", usage_text)
            self.assertIn("500", usage_text)
            self.assertIn("上下文窗口", usage_text)

    def test_models_account_cancel_and_low_quota_alerts(self):
        agy_store.save_runtime_env(
            {"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:50051", "ANTIGRAVITY_CSRF_TOKEN": "csrf-123"},
            data=self.data,
        )
        rpc_calls = []

        def fake_ls_rpc(addr, csrf, method, payload=None, timeout=4.0):
            rpc_calls.append((method, payload))
            if method == "GetCascadeModelConfigData":
                return {
                    "clientModelConfigs": [
                        {"modelId": "gemini-4-argon-high", "label": "Gemini 4 Argon (High)"},
                        {"modelId": "claude-opus-5-5-high", "label": "Claude Opus 5.5 (High)"},
                    ]
                }
            if method == "GetUserStatus":
                return {
                    "userStatus": {
                        "email": "dev@example.com",
                        "planStatus": {"planInfo": {"planName": "Pro"}},
                    }
                }
            if method in {"CancelCascadeInvocation", "ForceStopCascadeTree"}:
                return {}
            return None

        with patch.object(agy_store, "_call_ls_rpc", side_effect=fake_ls_rpc):
            models = agy_store.fetch_antigravity_models(data=self.data)
            self.assertEqual([m["model"] for m in models], ["gemini-4-argon-high", "claude-opus-5-5-high"])
            account = agy_store.fetch_antigravity_account(data=self.data)
            self.assertEqual(account, {"type": "chatgpt", "email": "dev@example.com", "planType": "Pro"})
            self.assertTrue(agy_store.cancel_antigravity_cascade(self.thread_id, data=self.data))
            self.assertIn(("CancelCascadeInvocation", {"cascadeId": self.thread_id}), rpc_calls)
            self.assertIn(("ForceStopCascadeTree", {"conversationId": self.thread_id}), rpc_calls)


if __name__ == "__main__":
    unittest.main()
