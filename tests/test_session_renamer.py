import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import sys

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import session_renamer as sr  # noqa: E402


class SessionRenamerTests(unittest.TestCase):
    def test_maintenance_backs_up_and_prunes_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            p = sr.paths(home)
            sr.ensure_dirs(p)
            (p["current"] / "old.txt").write_text("old", encoding="utf-8")
            cache = {
                "version": 1,
                "entries": {
                    "fresh": {"created_at": sr.iso_now(), "proposal": {}},
                    "old": {"created_at": "2020-01-01T00:00:00Z", "proposal": {}},
                },
            }
            sr.write_json(p["cache"], cache)
            args = type("Args", (), {"codex_home": str(home), "cache_days": 60, "keep_backups": 20})
            sr.run_maintenance(args)
            self.assertFalse((p["current"] / "old.txt").exists())
            self.assertTrue((p["current"] / "maintenance_report.json").exists())
            self.assertEqual(len(list(p["backups"].glob("*.zip"))), 1)
            pruned = sr.read_json(p["cache"])["entries"]
            self.assertIn("fresh", pruned)
            self.assertNotIn("old", pruned)

    def test_discover_parses_latest_remote_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "session_index.jsonl"
            index.write_text(
                "\n".join(
                    [
                        json.dumps({"id": "abc", "thread_name": "old", "updated_at": "2026-01-01T00:00:00Z"}),
                        json.dumps({"id": "abc", "thread_name": "new", "updated_at": "2026-01-02T00:00:00Z"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            sessions = sr.parse_session_index(index, "server1")
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["title"], "new")

    def test_title_validation_rejects_aggressive_change(self):
        ok, reason = sr.validate_new_title("Qwen ASR", "🎙️ Qwen ASR")
        self.assertTrue(ok, reason)
        ok, reason = sr.validate_new_title("CODEX 提权", "🔓 UAC白名单与快捷方式提权")
        self.assertTrue(ok, reason)
        ok, _ = sr.validate_new_title("Qwen ASR", "🎙️ Completely Different Project")
        self.assertFalse(ok)

    def test_extract_user_messages_skips_wrappers_and_uses_real_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "rollout-thread.jsonl"
            rows = [
                {"type": "session_meta", "payload": {"cwd": "C:\\work\\project"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "<environment_context>\nnoise\n</environment_context>"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "# Context from my IDE setup:\n## My request for Codex: 看一下 Image_Segmentation_FastAPI 项目"}],
                    },
                },
            ]
            transcript.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
            self.assertEqual(sr.extract_transcript_cwd(transcript), "C:\\work\\project")
            self.assertEqual(sr.extract_user_messages(transcript), ["看一下 Image_Segmentation_FastAPI 项目"])

    def test_prepare_apply_filters_rejected_and_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            p = sr.paths(home)
            sr.ensure_dirs(p)
            approvals = {
                "approvals": [
                    {
                        "threadId": "a",
                        "host": "local",
                        "oldTitle": "Qwen ASR",
                        "newTitle": "🎙️ Qwen ASR",
                        "approved": True,
                        "fingerprint": "f1",
                    },
                    {
                        "threadId": "b",
                        "host": "local",
                        "oldTitle": "Qwen ASR",
                        "newTitle": "No emoji",
                        "approved": True,
                    },
                    {
                        "threadId": "c",
                        "host": "local",
                        "oldTitle": "Gmail",
                        "newTitle": "📩 Gmail",
                        "approved": False,
                    },
                ]
            }
            sr.write_json(p["current"] / "approved.json", approvals)
            args = type("Args", (), {"codex_home": str(home), "approvals": None, "output": None})
            sr.run_prepare_apply(args)
            plan = sr.read_json(p["current"] / "apply_plan.json")
            self.assertEqual(plan["apply_count"], 1)
            self.assertEqual(plan["skip_count"], 2)

    def test_save_approvals_writes_desktop_apply_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            p = sr.paths(home)
            sr.ensure_dirs(p)
            saved = sr.save_approvals_plan_and_desktop_request(
                p["current"],
                {
                    "approvals": [
                        {
                            "threadId": "a",
                            "host": "server1",
                            "oldTitle": "Qwen ASR",
                            "newTitle": "🎙️ Qwen ASR",
                            "approved": True,
                        }
                    ]
                },
            )
            request = sr.read_json(p["current"] / "desktop_apply_request.json")
            self.assertEqual(saved["desktop_request"]["apply_count"], 1)
            self.assertEqual(request["requires_tool"], "codex_app.set_thread_title")
            self.assertEqual(request["counts_by_host"], {"server1": 1})
            self.assertTrue((p["current"] / "desktop_apply_status.json").exists())

    def test_subagent_request_writes_prompt_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            p = sr.paths(home)
            sr.ensure_dirs(p)
            request = sr.build_subagent_request(
                p["current"],
                {
                    "sessions": [
                        {
                            "threadId": "a",
                            "host": "server2",
                            "oldTitle": "查看项目",
                            "context": {"preview": "看看 Qwen ASR 服务", "context": "用户在排查 ASR 配置"},
                        }
                    ],
                    "backendConfig": {
                        "codex_model": "gpt-5.4-mini",
                        "codex_fallback_model": "gpt-5.3-codex-spark",
                    },
                },
            )
            self.assertEqual(request["count"], 1)
            self.assertEqual(request["preferred_model"], "gpt-5.4-mini")
            self.assertEqual(request["fallback_model"], "gpt-5.3-codex-spark")
            self.assertIn("gpt-5.4-mini", request["agent_instructions"][0])
            self.assertIn("gpt-5.3-codex-spark", request["agent_instructions"][0])
            self.assertTrue(Path(request["prompt_path"]).exists())
            status = sr.subagent_request_status(p["current"], request["request_id"])
            self.assertEqual(status["state"], "pending_codex_subagent")

    def test_subagent_request_accepts_safe_future_model_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            p = sr.paths(home)
            sr.ensure_dirs(p)
            request = sr.build_subagent_request(
                p["current"],
                {
                    "sessions": [{"threadId": "a", "host": "local", "oldTitle": "查看需求"}],
                    "backendConfig": {
                        "codex_model": "gpt-5.6-codex",
                        "codex_fallback_model": "../../bad",
                    },
                },
            )
            self.assertEqual(request["preferred_model"], "gpt-5.6-codex")
            self.assertEqual(request["fallback_model"], "gpt-5.4-mini")

    def test_start_review_request_does_not_persist_api_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            p = sr.paths(home)
            sr.ensure_dirs(p)
            saved = sr.write_start_review_request(
                p["current"],
                {
                    "backendConfig": {
                        "backend": "openai",
                        "openai_base_url": "https://api.example.test/v1",
                        "openai_model": "custom-model",
                        "openai_api_key": "secret-value",
                    }
                },
            )
            request = sr.read_json(saved["request_path"])
            self.assertEqual(request["backend_config"]["openai_base_url"], "https://api.example.test/v1")
            self.assertEqual(request["backend_config"]["openai_model"], "custom-model")
            self.assertEqual(request["backend_config"]["openai_api_key"], "[not stored]")
            self.assertNotIn("secret-value", json.dumps(request))

    def test_quickstart_mentions_review_first_and_safety(self):
        zh = sr.skill_quickstart_text("zh")
        en = sr.skill_quickstart_text("en")
        self.assertIn("先审核、后应用", zh)
        self.assertIn("生成审核页不会改名", zh)
        self.assertIn("review first, apply later", en)
        self.assertIn("agent-review", en)

    def test_clean_snippet_redacts_chinese_password_tokens(self):
        text = sr.clean_snippet("sudo 密码 li3.141592li，密码告诉你是 li3secret")
        self.assertNotIn("li3.141592li", text)
        self.assertNotIn("li3secret", text)
        self.assertIn("[redacted]", text)

    def test_model_prompts_redact_cwd_basename(self):
        session = {
            "threadId": "abc",
            "host": "local",
            "title": "VPS迁移",
            "cwd": r"C:\Users\davidli\Project\vps-li3-141592li",
            "preview": "密码告诉你是 li3secret",
            "contextSnippet": "sudo password li3secret",
        }
        context = sr.proposal_context(session)
        prompt = sr.build_subagent_prompt([session])
        serialized = json.dumps(context, ensure_ascii=False) + prompt
        self.assertNotIn("vps-li3-141592li", serialized)
        self.assertNotIn("li3secret", serialized)

    def test_agent_review_runs_scriptable_workflow_and_writes_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            p = sr.paths(home)
            sr.ensure_dirs(p)
            local_json = p["current"] / "local_threads.json"
            request_json = p["current"] / "start_review_request.json"
            sr.write_json(
                local_json,
                {
                    "threads": [
                        {
                            "id": "abc",
                            "title": "Qwen ASR",
                            "preview": "排查 Qwen ASR 配置",
                        }
                    ]
                },
            )
            sr.write_json(request_json, {"backend_config": {"backend": "codex"}})
            args = type(
                "Args",
                (),
                {
                    "codex_home": str(home),
                    "request": str(request_json),
                    "local_json": str(local_json),
                    "backend": "heuristic",
                    "subagent_json": None,
                    "remote_index": None,
                    "ssh_host": None,
                    "vllm_base_url": None,
                    "vllm_api_key": None,
                    "model": None,
                    "timeout": 30,
                    "batch_size": 1,
                    "port": 8765,
                    "cache_days": 60,
                    "keep_backups": 20,
                },
            )
            sr.run_agent_review(args)
            status = sr.read_json(p["current"] / "agent_review_status.json")
            self.assertEqual(status["state"], "review_ready")
            self.assertTrue((p["current"] / "review.html").exists())
            self.assertTrue((p["current"] / "agent_review_commands.md").exists())
            self.assertIn("agent-review", (p["current"] / "agent_review_commands.md").read_text(encoding="utf-8"))

    def test_subagent_json_bypasses_stale_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            p = sr.paths(home)
            sr.ensure_dirs(p)
            session = {"threadId": "abc", "host": "local", "title": "Qwen ASR"}
            session["fingerprint"] = sr.session_fingerprint(session)
            sr.write_json(p["current"] / "sessions.json", {"sessions": [session]})
            cache = sr.load_cache(p["cache"])
            stale = {
                "threadId": "abc",
                "host": "local",
                "oldTitle": "Qwen ASR",
                "newTitle": "❓ Stale Title",
                "reason": "stale",
            }
            cache["entries"][sr.cache_key(session, "subagent")] = {"created_at": sr.iso_now(), "proposal": stale}
            sr.write_json(p["cache"], cache)
            result_path = p["current"] / "subagent_proposals.json"
            sr.write_json(result_path, {"renames": [{"id": "abc", "new_title": "🎙️ Qwen ASR", "reason": "fresh"}]})
            args = type(
                "Args",
                (),
                {
                    "codex_home": str(home),
                    "input": None,
                    "output": None,
                    "backend": "subagent",
                    "vllm_base_url": sr.DEFAULT_VLLM_BASE_URL,
                    "vllm_api_key": sr.DEFAULT_VLLM_API_KEY,
                    "model": None,
                    "timeout": 30,
                    "batch_size": 1,
                    "subagent_json": str(result_path),
                },
            )
            sr.run_propose(args)
            proposals = sr.read_json(p["current"] / "proposals.json")
            self.assertEqual(proposals["backend_used"], "subagent")
            self.assertEqual(proposals["cache_hits"], 0)
            self.assertEqual(proposals["proposals"][0]["newTitle"], "🎙️ Qwen ASR")

    def test_package_excludes_git_and_zip_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "skill.zip"
            args = type("Args", (), {"output": str(output)})
            sr.run_package(args)
            with zipfile.ZipFile(output, "r") as zf:
                names = zf.namelist()
            self.assertTrue(any(name.endswith("SKILL.md") for name in names))
            self.assertFalse(any("/.git/" in name or name.startswith(".git/") for name in names))
            self.assertFalse(any(name.endswith(".pyc") or name.endswith(".zip") for name in names))

    def test_render_review_escapes_script_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            p = sr.paths(home)
            sr.ensure_dirs(p)
            sr.write_json(
                p["current"] / "proposals.json",
                {
                    "count": 1,
                    "proposals": [
                        {
                            "threadId": "x",
                            "host": "local",
                            "oldTitle": "</script>",
                            "newTitle": "❓ </script>",
                            "reason": "escape test",
                            "valid": True,
                        }
                    ],
                },
            )
            args = type("Args", (), {"codex_home": str(home), "input": None, "output": None})
            sr.run_render_review(args)
            html = (p["current"] / "review.html").read_text(encoding="utf-8")
            self.assertIn("<\\/script>", html)
            self.assertIn('id="startReview"', html)
            self.assertIn("buildStartReviewPrompt", html)
            self.assertIn('id="editAssistantPrompt"', html)
            self.assertIn('id="resetAssistantPrompt"', html)
            self.assertIn("syncAssistantPromptLanguage", html)
            self.assertIn('id="modelBackend"', html)
            self.assertIn('<select id="codexModel"', html)
            self.assertNotIn('list="codexModelOptions"', html)
            self.assertIn('value="unavailable"', html)
            self.assertIn("isUnavailableSkipped", html)


if __name__ == "__main__":
    unittest.main()
