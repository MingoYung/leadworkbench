import copy
import json
import unittest
from unittest import mock

from leadworkbench import collectors, comments


WB = "https://weibo.com/detail/123456789"
XHS = "https://www.xiaohongshu.com/explore/abcdef123456?xsec_token=fixture"
BILI = "https://www.bilibili.com/video/BV1abcDEF123"
ZHIHU = "https://www.zhihu.com/question/123/answer/456"


def consume(stream):
    records = []
    while True:
        try:
            records.append(next(stream))
        except StopIteration as done:
            return records, done.value


class CommentIdentityTests(unittest.TestCase):
    def test_same_post_retains_distinct_comments_without_fabricating_links(self):
        source = [{"source_url": WB, "kind": "comment", "comment_id": "1", "content": "想找装修团队", "author_handle": "评论者甲"},
                  {"source_url": WB, "kind": "comment", "comment_id": "2", "content": "旧房翻新求推荐", "author_handle": "评论者乙"}]
        untouched = copy.deepcopy(source)
        records, warnings = collectors.normalize_records(source)
        self.assertEqual(2, len(records))
        self.assertFalse(warnings)
        self.assertNotEqual(collectors.record_identity(records[0]), collectors.record_identity(records[1]))
        self.assertEqual(WB, records[0]["source_url"])
        self.assertNotIn("#comment", records[0]["source_url"])
        self.assertEqual(untouched, source)

    def test_missing_native_ids_use_explicit_observation_fingerprint(self):
        source = [{"source_url": WB, "kind": "comment", "content": "成都旧房翻新", "author_handle": "甲"},
                  {"source_url": WB, "kind": "comment", "content": "成都旧房翻新", "author_handle": "乙"}]
        records, warnings = collectors.normalize_records(source)
        self.assertEqual(2, len(records))
        self.assertFalse(warnings)
        self.assertIsNone(records[0]["comment_id"])
        self.assertEqual("observed_content_fingerprint", records[0]["metadata"]["comment_identity_strategy"])
        duplicate = dict(source[0], retrieved_at="2030-01-01T00:00:00Z")
        self.assertEqual(collectors.record_identity(records[0]), collectors.record_identity(duplicate))

    def test_parent_title_never_becomes_comment_content(self):
        records, _ = collectors.normalize_records([{"source_url": WB, "kind": "comment", "title": "父帖品牌广告", "content": "我是业主，想装修", "metadata": {"parent_title": "父帖品牌广告", "comment_id": "1"}}])
        self.assertEqual("我是业主，想装修", records[0]["content"])
        self.assertEqual("父帖品牌广告", records[0]["metadata"]["parent_title"])
        self.assertFalse(records[0].get("brand_name"))

    def test_catalog_has_only_verified_plural_read_capabilities(self):
        catalog = {p["id"]: p for p in collectors.platform_catalog()}
        for platform in ("weibo", "xiaohongshu", "bilibili", "zhihu"):
            self.assertTrue(catalog[platform]["comments_supported"])
        for platform in ("douyin", "kuaishou", "tiktok", "instagram"):
            self.assertFalse(catalog[platform]["comments_supported"])
        self.assertFalse(catalog["weibo"]["comment_replies_supported"])


class CommentReadTests(unittest.TestCase):
    def run_with(self, urls, platforms, responses, options=None, limit=20):
        calls = []
        def read(argv, config, timeout_cap=180):
            calls.append(argv)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return {"ok": True, "data": item}
        with mock.patch.object(collectors.shutil, "which", return_value="/native/opencli"), mock.patch.object(collectors, "_run_opencli_json", side_effect=read):
            records, summary = consume(collectors.collect_comments(urls, platforms, limit, {}, options or {}))
        self.assertFalse(responses)
        return calls, records, summary

    def test_weibo_reads_multiple_comments_and_locally_filters_keywords(self):
        calls, records, summary = self.run_with([{"source_url": WB, "title": "父帖装修案例", "author_handle": "父帖作者"}], ["weibo"], [[
            {"author": "业主甲", "text": "成都旧房装修，求推荐", "time": "今天 10:00"},
            {"author": "业主乙", "text": "成都装修想问预算", "time": "今天 10:01"},
            {"author": "路人", "text": "好看", "time": "今天 10:02"}]], {"keywords": ["成都", "装修"], "match_mode": "all"})
        self.assertEqual(["/native/opencli", "weibo", "comments", "123456789", "--limit", "20"], calls[0])
        self.assertEqual(2, len(records))
        self.assertEqual("业主甲", records[0]["author_handle"])
        self.assertNotIn("父帖作者", records[0]["content"])
        self.assertEqual("父帖装修案例", records[0]["metadata"]["parent_title"])
        self.assertFalse(records[0]["metadata"]["comment_permalink_available"])
        self.assertEqual(3, summary["comments_read"])
        self.assertEqual(2, summary["comments_matched"])

    def test_zero_keyword_matches_is_normal_bounded_summary(self):
        _, records, summary = self.run_with([WB], ["weibo"], [[{"author": "路人", "text": "喜欢这个设计"}]], {"keywords": ["装修公司"]})
        self.assertEqual([], records)
        self.assertEqual(1, summary["comments_read"])
        self.assertEqual(0, summary["comments_matched"])
        self.assertFalse(summary["global_comment_index"])
        self.assertEqual("selected_post_comment_sample", summary["scope"])

    def test_case_is_preserved_and_keyword_match_is_case_insensitive(self):
        _, records, _ = self.run_with([WB], ["weibo"], [[{"author": "Buyer", "text": "Need DIY Renovation advice"}]], {"keywords": ["diy"]})
        self.assertEqual("Need DIY Renovation advice", records[0]["content"])

    def test_weibo_bid_resolves_via_verified_read_post_not_write_comment(self):
        calls, records, _ = self.run_with(["https://weibo.com/123/AbCd"], ["weibo"], [[{"field": "id", "value": "987654321"}, {"field": "text", "value": "父帖原文"}], [{"author": "业主", "text": "我想找施工队"}]])
        self.assertEqual(["/native/opencli", "weibo", "post", "AbCd"], calls[0])
        self.assertEqual("987654321", calls[1][3])
        self.assertEqual("父帖原文", records[0]["metadata"]["parent_title"])
        self.assertTrue(all("comment" not in argv for argv in calls))

    def test_xhs_reply_flag_and_author_profile_are_not_comment_permalink(self):
        calls, records, summary = self.run_with([XHS], ["xiaohongshu"], [[{"author": "评论甲", "userId": "abc", "profileUrl": "https://www.xiaohongshu.com/user/profile/abc", "text": "成都装修求助", "time": "昨天", "is_reply": False}, {"author": "评论乙", "text": "同求", "time": "昨天", "is_reply": True, "reply_to": "评论甲"}]], {"include_replies": True})
        self.assertEqual(["--with-replies", "true"], calls[0][-2:])
        self.assertEqual(2, len(records))
        self.assertEqual(XHS, records[0]["source_url"])
        self.assertIsNone(records[1]["parent_comment_id"])
        self.assertEqual("评论甲", records[1]["metadata"]["reply_to_display"])
        self.assertEqual("bounded_rendered_dom", summary["post_results"][0]["pagination"])

    def test_zhihu_unchecked_replies_explicitly_disables_native_default(self):
        calls, records, _ = self.run_with([ZHIHU], ["zhihu"], [[{"id": "10", "parent_id": "", "depth": 0, "author": "评论甲", "content": "成都找施工队", "created_at": "2026-11-01T00:00:00Z", "url": ZHIHU + "#comment-10"}]])
        self.assertEqual("answer-comments", calls[0][2])
        self.assertEqual(["--replies-limit", "0"], calls[0][-2:])
        self.assertEqual("10", records[0]["comment_id"])
        self.assertEqual(ZHIHU, records[0]["source_url"])
        self.assertEqual(ZHIHU + "#comment-10", records[0]["metadata"]["provider_reported_comment_url"])

    def test_zhihu_preserves_real_reply_parent_ids(self):
        calls, records, _ = self.run_with([ZHIHU], ["zhihu"], [[{"id": "10", "parent_id": "", "depth": 0, "author": "甲", "content": "求推荐"}, {"id": "11", "parent_id": "10", "depth": 1, "author": "乙", "content": "我也需要装修"}]], {"include_replies": True})
        self.assertEqual(["--replies-limit", "3"], calls[0][-2:])
        self.assertEqual("10", records[1]["parent_comment_id"])
        self.assertTrue(records[1]["metadata"]["is_reply"])

    def test_bilibili_nested_replies_are_bounded_and_direct_parent_not_invented(self):
        calls, records, _ = self.run_with([BILI], ["bilibili"], [[{"rpid": "100", "author": "甲", "text": "找装修", "replies": 4}], [{"rpid": "101", "author": "乙", "text": "我也找", "replies": 0}]], {"include_replies": True, "max_comments_per_post": 4})
        self.assertIn("--parent", calls[1])
        self.assertEqual("100", calls[1][calls[1].index("--parent") + 1])
        self.assertEqual("101", records[1]["comment_id"])
        self.assertIsNone(records[1]["parent_comment_id"])
        self.assertEqual("100", records[1]["metadata"]["root_comment_id"])

    def test_native_caps_partial_flags_and_replies_limitations_are_visible(self):
        calls, records, summary = self.run_with([WB], ["weibo"], [[{"author": "甲", "text": "要装修"}]], {"max_comments_per_post": 100, "include_replies": True})
        self.assertEqual("50", calls[0][-1])
        self.assertTrue(records[0]["is_partial"])
        self.assertFalse(records[0]["metadata"]["all_comments_read"])
        self.assertFalse(summary["post_results"][0]["replies_supported"])

    def test_invalid_target_and_options_never_become_write_commands(self):
        with mock.patch.object(collectors.shutil, "which", return_value="/native/opencli"), mock.patch.object(collectors, "_run_opencli_json") as read:
            for url in ["https://www.zhihu.com/question/123", "https://www.xiaohongshu.com/explore/abc", "https://user:password@weibo.com/detail/123", "https://evil.example/status/123"]:
                with self.assertRaises(collectors.CollectionError):
                    consume(collectors.collect_comments([url], None, 20, {}))
            for options in [{"include_replies": "false"}, {"match_mode": "regex"}, {"keywords": [""]}, {"max_posts": 21}]:
                with self.assertRaises(collectors.CollectionError):
                    consume(collectors.collect_comments([WB], ["weibo"], 20, {}, options))
            with self.assertRaises(collectors.CollectionError):
                consume(collectors.collect_comments([WB], ["weibo"], 20, {"action": "comment"}))
            read.assert_not_called()

    def test_partial_error_keeps_records_and_attaches_summary(self):
        responses = [[{"author": "甲", "text": "找施工队"}], collectors.CollectionError("需要登录", "opencli_auth_required")]
        seen = []
        def read(*args, **kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return {"ok": True, "data": value}
        with mock.patch.object(collectors.shutil, "which", return_value="/native/opencli"), mock.patch.object(collectors, "_run_opencli_json", side_effect=read):
            with self.assertRaises(collectors.CollectionError) as caught:
                for record in collectors.collect_comments([WB, XHS], ["weibo", "xiaohongshu"], 20, {}):
                    seen.append(record)
        self.assertEqual(1, len(seen))
        self.assertEqual(1, caught.exception.collection_summary["comments_returned"])
        self.assertEqual(2, caught.exception.collection_summary["posts_attempted"])

    def test_bilibili_reply_failure_keeps_root_comments(self):
        responses = [[{"rpid": "100", "author": "甲", "text": "装修求助", "replies": 1}], collectors.CollectionError("bridge unavailable", "opencli_browser_unavailable")]
        seen = []
        def read(*args, **kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return {"ok": True, "data": value}
        with mock.patch.object(collectors.shutil, "which", return_value="/native/opencli"), mock.patch.object(collectors, "_run_opencli_json", side_effect=read):
            with self.assertRaises(collectors.CollectionError):
                for record in collectors.collect_comments([BILI], ["bilibili"], 20, {}, {"include_replies": True}):
                    seen.append(record)
        self.assertEqual(1, len(seen))
        self.assertIn("comment_read_errors", seen[0]["metadata"])


if __name__ == "__main__":
    unittest.main()
