import json
import unittest
import urllib.parse
from unittest import mock

from leadworkbench import collectors


DOMESTIC_ROWS = {
    "douyin": {"url": "https://www.douyin.com/video/123", "desc": "成都新房交付，准备找装修团队", "author": "示例业主", "likes": 95},
    "weibo": {"url": "https://weibo.com/123/abc", "title": "成都老房翻新想找施工队", "author": "示例用户", "time": "今天 10:30"},
    "xiaohongshu": {"url": "https://www.xiaohongshu.com/explore/abc?xsec_token=public-note-link", "title": "成都新房装修求建议", "author": "示例作者", "published_at": "2026-10-31"},
    "bilibili": {"url": "https://www.bilibili.com/video/BVfixture", "title": "旧房改造记录", "desc": "还在选择装修团队", "author": "示例UP主", "pubdate_ts": 1761984000},
    "zhihu": {"url": "https://www.zhihu.com/question/123", "title": "成都装修公司该怎么选？", "type": "question", "author": "示例提问者"},
    "tieba": {"url": "https://tieba.baidu.com/p/123", "title": "成都找装修师傅", "snippet": "旧房需要翻新，求建议", "forum": "成都", "author": "示例楼主", "time": "10-31"},
}


class DomesticMappingTests(unittest.TestCase):
    def test_catalog_is_detached_and_experimental_status_truthful(self):
        catalog = collectors.platform_catalog()
        sites = {p["id"]: p for p in catalog}
        self.assertEqual(len(catalog), len(sites))
        self.assertTrue(sites["kuaishou"]["search_supported"])
        self.assertTrue(sites["kuaishou"]["experimental"])
        self.assertIn("尚未", sites["kuaishou"]["limitations"])
        self.assertEqual("cn", sites["douyin"]["market"])
        self.assertEqual("global", sites["tiktok"]["market"])
        catalog[0]["label"] = "mutated"
        self.assertNotEqual("mutated", collectors.platform_catalog()[0]["label"])

    def test_search_records_preserve_literal_body_and_author(self):
        for platform, item in DOMESTIC_ROWS.items():
            with self.subTest(platform=platform):
                mapped = collectors._opencli_record(item, platform, "search", "成都装修")
                records, warnings = collectors.normalize_records([mapped])
                self.assertFalse(warnings)
                record = records[0]
                self.assertEqual(item["url"], record["source_url"])
                self.assertEqual(item["author"], record["author_handle"])
                self.assertTrue(record["is_partial"])
                self.assertFalse(record["is_demo"])
                self.assertNotIn("95", record["content"])
                self.assertNotIn("url:", record["content"])

    def test_xiaohongshu_date_inference_does_not_become_posted_at(self):
        mapped = collectors._opencli_record(DOMESTIC_ROWS["xiaohongshu"], "xiaohongshu", "search", "成都")
        records, _ = collectors.normalize_records([mapped])
        record = records[0]
        self.assertIsNone(record["posted_at"])
        self.assertEqual("2026-10-31", record["metadata"]["upstream_inferred_published_at"])
        self.assertEqual("note_id_inference_not_verified", record["metadata"]["date_source"])
        self.assertEqual("成都新房装修求建议", record["content"])

    def test_observed_time_and_bilibili_timestamp_are_distinct(self):
        wb = collectors._opencli_record(DOMESTIC_ROWS["weibo"], "weibo", "search", "x")
        bili = collectors._opencli_record(DOMESTIC_ROWS["bilibili"], "bilibili", "search", "x")
        self.assertEqual("今天 10:30", wb["posted_at"])
        self.assertEqual("visible_time_text", wb["metadata"]["date_source"])
        self.assertTrue(bili["posted_at"].endswith("+00:00"))
        self.assertEqual(1761984000, bili["metadata"]["pubdate_ts"])


class NativeReadTransportTests(unittest.TestCase):
    def test_domestic_commands_use_native_read_argv_and_per_site_caps(self):
        for platform in DOMESTIC_ROWS:
            calls = []
            def launch(argv, **kwargs):
                self.assertFalse(kwargs["shell"])
                calls.append(argv)
                kwargs["stdout"].write(json.dumps({"ok": True, "data": [DOMESTIC_ROWS[platform]]}).encode())
                process = mock.Mock(returncode=0)
                process.wait.return_value = 0
                return process
            with self.subTest(platform=platform), mock.patch.object(collectors.shutil, "which", return_value="/native/opencli"), mock.patch.object(collectors.subprocess, "Popen", side_effect=launch):
                records = list(collectors.collect_opencli(["成都装修 & literal"], [platform], 50, {"profile": "research"}))
                cap = {"douyin": "30", "tieba": "20"}.get(platform, "50")
                self.assertEqual(["/native/opencli", "--profile", "research", platform, "search", "成都装修 & literal", "--limit", cap], calls[0])
                self.assertEqual(1, len(records))

    def test_platform_mislabel_missing_url_and_unsafe_action_are_errors(self):
        payloads = [({"id": "123", "title": "找施工队"}, "opencli_missing_evidence"), ({"url": "https://unrelated.example/post", "title": "找施工队"}, "opencli_invalid_source")]
        for row, expected in payloads:
            with mock.patch.object(collectors, "_run_opencli_json", return_value={"data": [row]}):
                with self.assertRaises(collectors.CollectionError) as caught:
                    collectors._read_opencli_job([], "weibo", "search", "装修", 2, {})
            self.assertEqual(expected, caught.exception.code)
        with mock.patch.object(collectors.subprocess, "Popen") as launch:
            for action in ["comment", "user", "profile", []]:
                with self.assertRaises(collectors.CollectionError):
                    list(collectors.collect_opencli(["装修"], ["weibo"], 2, {"action": action}))
            launch.assert_not_called()

    def test_query_that_looks_like_an_option_is_not_executed(self):
        with mock.patch.object(collectors.shutil, "which", return_value="/native/opencli"), mock.patch.object(collectors.subprocess, "Popen") as launch:
            for query in ["--profile other", "x\ncomment", "\x00"]:
                with self.assertRaises(collectors.CollectionError):
                    list(collectors.collect_opencli([query], ["weibo"], 3, {}))
            launch.assert_not_called()

    def test_error_messages_are_actionable_without_echoing_upstream_secrets(self):
        for number, code, expected in [(69, "BROWSER_CONNECT", "opencli_browser_unavailable"), (77, "AUTH_REQUIRED", "opencli_auth_required"), (75, "TIMEOUT", "opencli_timeout"), (66, "EMPTY_RESULT", "opencli_empty_result"), (78, "CONFIG", "opencli_config_error")]:
            raw = json.dumps({"ok": False, "error": {"code": code, "message": "secret-fixture-token", "url": "https://example.invalid/?token=secret"}}).encode()
            error = collectors._opencli_exit_error(number, raw)
            self.assertEqual(expected, error.code)
            self.assertNotIn("secret", str(error))

    def test_successful_empty_response_is_not_proof_of_no_demand(self):
        with mock.patch.object(collectors, "_run_opencli_json", return_value={"ok": True, "data": []}):
            with self.assertRaises(collectors.CollectionError) as caught:
                collectors._read_opencli_job([], "weibo", "search", "装修", 2, {})
        self.assertEqual("opencli_empty_result", caught.exception.code)


class FairCollectionTests(unittest.TestCase):
    def test_query_then_platform_round_robin_shares_budget(self):
        calls = []
        def read(prefix, platform, action, query, requested, config):
            calls.append((platform, query, requested))
            return [{"source_url": f"https://{platform}.invalid/{query}", "metadata": {}} for _ in range(requested)]
        with mock.patch.object(collectors.shutil, "which", return_value="/native/opencli"), mock.patch.object(collectors, "_read_opencli_job", side_effect=read):
            records = list(collectors.collect_opencli(["q1", "q2"], ["douyin", "weibo", "xiaohongshu"], 6, {}))
        self.assertEqual([("douyin", "q1", 1), ("weibo", "q1", 1), ("xiaohongshu", "q1", 1), ("douyin", "q2", 1), ("weibo", "q2", 1), ("xiaohongshu", "q2", 1)], calls)
        self.assertEqual(6, len(records))
        self.assertEqual(6, records[-1]["metadata"]["planned_jobs"])

    def test_one_platform_auth_failure_preserves_other_platforms_results(self):
        calls = []
        def read(prefix, platform, action, query, requested, config):
            calls.append((platform, query))
            if platform == "douyin":
                raise collectors.CollectionError("login needed", "opencli_auth_required")
            return [{"source_url": f"https://weibo.invalid/{query}", "metadata": {}}]
        saved = []
        with mock.patch.object(collectors.shutil, "which", return_value="/native/opencli"), mock.patch.object(collectors, "_read_opencli_job", side_effect=read):
            with self.assertRaises(collectors.CollectionError) as caught:
                for record in collectors.collect_opencli(["q1", "q2"], ["douyin", "weibo"], 5, {}):
                    saved.append(record)
        self.assertEqual("opencli_auth_required", caught.exception.code)
        self.assertEqual(2, len(saved))
        self.assertEqual([("douyin", "q1"), ("weibo", "q1"), ("weibo", "q2")], calls)

    def test_global_bridge_failure_is_not_retried_for_every_query(self):
        error = collectors.CollectionError("bridge missing", "opencli_browser_unavailable")
        with mock.patch.object(collectors.shutil, "which", return_value="/native/opencli"), mock.patch.object(collectors, "_read_opencli_job", side_effect=error) as read:
            with self.assertRaises(collectors.CollectionError):
                list(collectors.collect_opencli(["q1", "q2"], ["douyin", "weibo"], 5, {}))
            read.assert_called_once()

    def test_platforms_skipped_by_too_small_budget_are_reported(self):
        def read(prefix, platform, action, query, requested, config):
            return [{"source_url": f"https://{platform}.invalid/{query}", "metadata": {}}]
        saved = []
        with mock.patch.object(collectors.shutil, "which", return_value="/native/opencli"), mock.patch.object(collectors, "_read_opencli_job", side_effect=read):
            with self.assertRaises(collectors.CollectionError) as caught:
                for record in collectors.collect_opencli(["q1"], ["douyin", "weibo"], 1, {}):
                    saved.append(record)
        self.assertEqual("opencli_platforms_skipped", caught.exception.code)
        self.assertEqual(1, len(saved))


class ExperimentalKuaishouTests(unittest.TestCase):
    def fake(self, state="ok", page="owned:123", close_error=False):
        calls = []
        def request(argv, config, timeout_cap=180):
            calls.append(argv)
            if "new" in argv:
                return {"ok": True, "data": {"url": argv[-1], "page": page}}
            if "eval" in argv:
                return {"ok": True, "data": {"state": state, "rows": [{"title": "成都装修求建议", "content": "成都新房交付，找施工团队。", "url": "https://www.kuaishou.com/short-video/fixture123", "is_partial": True}]}}
            if "close" in argv and close_error:
                raise collectors.CollectionError("cleanup unavailable", "opencli_timeout")
            return {"ok": True, "data": {"closed": page}}
        return calls, request

    def test_owns_new_tab_and_reads_only_fixed_dom_script(self):
        calls, request = self.fake()
        query = "成都装修 & literal'quote"
        with mock.patch.object(collectors, "_run_opencli_json", side_effect=request):
            records = collectors._read_opencli_job(["/native/opencli"], "kuaishou", "search", query, 2, {})
        self.assertEqual(4, len(calls))
        self.assertIn("new", calls[0])
        self.assertEqual("https://www.kuaishou.com/search/video?searchKey=" + urllib.parse.quote(query, safe=""), calls[0][-1])
        script = calls[1][calls[1].index("eval") + 1]
        self.assertEqual(collectors._KUAISHOU_DOM_JS.replace("__LIMIT__", "2"), script)
        self.assertNotIn(query, script)
        for forbidden in ("fetch(", "XMLHttpRequest", "document.cookie", "localStorage", "click("):
            self.assertNotIn(forbidden, script)
        self.assertEqual(["--tab", "owned:123"], calls[1][-2:])
        self.assertEqual(["tab", "close", "owned:123"], calls[2][-3:])
        self.assertEqual("unbind", calls[3][-1])
        self.assertEqual(calls[0][calls[0].index("--session") + 1], calls[3][calls[3].index("--session") + 1])
        self.assertTrue(records[0]["metadata"]["experimental"])
        self.assertTrue(records[0]["is_partial"])

    def test_login_challenge_and_missing_selectors_fail_and_close_own_tab(self):
        for state, code in [("login_required", "opencli_auth_required"), ("challenge", "kuaishou_challenge"), ("no_readable_cards", "kuaishou_dom_unavailable"), ("wrong_page", "kuaishou_dom_unavailable")]:
            calls, request = self.fake(state)
            with self.subTest(state=state), mock.patch.object(collectors, "_run_opencli_json", side_effect=request):
                with self.assertRaises(collectors.CollectionError) as caught:
                    collectors._collect_kuaishou_rows([], "装修", 2, {})
            self.assertEqual(code, caught.exception.code)
            self.assertEqual(["tab", "close", "owned:123"], calls[2][-3:])

    def test_no_unknown_tab_is_closed_and_cleanup_error_does_not_hide_results(self):
        for page in (None, "--all", "owned/page", "owned page", "owned\n123"):
            calls, request = self.fake(page=page)
            with self.subTest(page=page), mock.patch.object(collectors, "_run_opencli_json", side_effect=request):
                with self.assertRaises(collectors.CollectionError) as caught:
                    collectors._collect_kuaishou_rows([], "装修", 2, {})
            self.assertEqual("kuaishou_tab_unverified", caught.exception.code)
            self.assertEqual(2, len(calls))
            self.assertEqual("unbind", calls[-1][-1])
            self.assertEqual(calls[0][calls[0].index("--session") + 1], calls[-1][calls[-1].index("--session") + 1])
            self.assertFalse(any("close" in argv or "eval" in argv for argv in calls))
        calls, request = self.fake(close_error=True)
        with mock.patch.object(collectors, "_run_opencli_json", side_effect=request):
            records = collectors._read_opencli_job([], "kuaishou", "search", "装修", 2, {})
        self.assertIn("cleanup_warning", records[0]["metadata"])
        self.assertEqual("unbind", calls[-1][-1])


if __name__ == "__main__":
    unittest.main()
