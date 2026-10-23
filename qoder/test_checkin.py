# -*- coding: utf8 -*-
"""qoderCN_checkin.py 的离线单元测试：不联网，全部用假 session 驱动。

运行：
  python -m unittest test_checkin -v
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qoderCN_checkin as checkin
import qoderINTL_checkin as intl


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status_code, doc=None):
        self.status_code = status_code
        self._doc = doc

    def json(self):
        return self._doc


class FakeSession:
    """按请求路径路由的假会话，记录所有调用。"""

    def __init__(self, campaigns=None, claim_handlers=None, usage=None):
        self.campaigns = campaigns or []
        self.claim_handlers = claim_handlers or {}
        self.usage = usage if usage is not None else {"qoderUsage": {}}
        self.calls = []

    def request(self, method, url, **kwargs):
        path = url.split("openapi.qoder.com.cn", 1)[-1]
        self.calls.append((method, path))
        if path == checkin.PATH_EXCHANGE:
            return FakeResponse(200, {"token": "jt-test"})
        if path == checkin.PATH_CAMPAIGNS and method == "GET":
            return FakeResponse(200, {"campaigns": self.campaigns})
        if path == checkin.PATH_USAGE:
            return FakeResponse(200, self.usage)
        if path.startswith(checkin.PATH_CAMPAIGNS + "/") and path.endswith("/claim"):
            campaign_id = path.split("/")[-2]
            handler = self.claim_handlers.get(campaign_id)
            if handler is None:
                return FakeResponse(200, {"grantId": "g-" + campaign_id})
            return handler()
        raise AssertionError("unexpected request: %s %s" % (method, path))

    def claim_calls(self):
        return [path for _, path in self.calls if path.endswith("/claim")]


def campaign(cid, amount=100, status="CLAIMABLE", action="CLAIM_BENEFIT", key=None):
    item = {
        "campaignId": cid,
        "actionType": action,
        "claimStatus": status,
        "benefit": {"amount": amount},
    }
    if key:
        item["campaignKey"] = key
    return item


def timed(cid, start, end, status="CLAIMABLE", key=None):
    item = campaign(cid, status=status, key=key)
    item["startAt"] = start
    item["endAt"] = end
    return item


class TestPureFunctions(unittest.TestCase):
    def test_region_endpoints_are_separated(self):
        self.assertEqual(checkin.API, "https://openapi.qoder.com.cn")
        self.assertEqual(intl.API, "https://openapi.qoder.sh")
        self.assertIn("QODERCN_PAT", checkin.MSG_AUTH_FAILED)
        self.assertIn("QODER_PAT", intl.MSG_AUTH_FAILED)

    def test_intl_daily_campaign_filtered_by_amount(self):
        daily = campaign("d", amount=100)
        pack = campaign("p", amount=2048)
        pack["campaignKey"] = "act-20261022-218"
        self.assertTrue(intl.is_daily_campaign(daily))
        self.assertFalse(intl.is_daily_campaign(pack))
        self.assertEqual([c["campaignId"] for c in intl.ordered_claimable([pack, daily])], ["d"])

    def test_intl_has_claimed_today_ignores_other_rewards(self):
        now = utc(2026, 10, 23, 3, 0)
        pack = campaign("p", amount=2048, status="CLAIMED")
        self.assertFalse(intl.has_claimed_today([pack], now))
        self.assertTrue(intl.has_claimed_today([campaign("d", amount=100, status="CLAIMED")], now))

    def test_parse_pats(self):
        self.assertEqual(
            checkin.parse_pats(" pt-a\r\npt-b & pt-a && pt-c "),
            ["pt-a", "pt-b", "pt-c"],
        )
        self.assertEqual(checkin.parse_pats(""), [])
        self.assertEqual(checkin.parse_pats(None), [])

    def test_current_campaign_key_window_boundary(self):
        # 10:00 前仍属于前一天开启的窗口
        self.assertEqual(
            checkin.current_campaign_key(utc(2026, 10, 22, 23, 59)), "act-20261022"
        )
        self.assertEqual(
            checkin.current_campaign_key(utc(2026, 10, 23, 1, 59)), "act-20261022"
        )
        self.assertEqual(
            checkin.current_campaign_key(utc(2026, 10, 23, 2, 0)), "act-20261023"
        )

    def test_pick_claimable(self):
        items = [
            campaign("a"),
            campaign("b", status="CLAIMED"),
            campaign("c", action="VIEW_DETAILS"),
            campaign(""),
            "not-a-dict",
        ]
        self.assertEqual([c["campaignId"] for c in checkin.pick_claimable(items)], ["a"])
        self.assertEqual(checkin.pick_claimable(None), [])

    def test_ordered_claimable_dedups_and_prefers_current_window(self):
        now = utc(2026, 10, 23, 3, 0)
        items = [
            campaign("old", key="act-20261022-001"),
            campaign("today", key="act-20261023-001"),
            campaign("today", key="act-20261023-001"),
            campaign("unknown"),
        ]
        ordered = checkin.ordered_claimable(items, now)
        self.assertEqual([c["campaignId"] for c in ordered], ["today", "unknown", "old"])

    def test_timestamps_take_priority_over_campaign_key(self):
        # 真实数据：开窗日 10-22，campaignKey 却是 act-20261020-993
        now = utc(2026, 10, 22, 17, 42)
        item = timed("daily", 1792634400, 1792720740, key="act-20261020-993")
        self.assertTrue(checkin.is_campaign_active(item, now))
        self.assertTrue(checkin.has_claimed_today([timed("d", 1792634400, 1792720740, status="CLAIMED")], now))
        self.assertEqual(checkin.ordered_claimable([item], now), [item])

    def test_expired_and_future_campaigns_are_excluded(self):
        now = utc(2026, 10, 22, 17, 42)
        expired = timed("old", 1792634400 - 86400, 1792634400 - 1)
        future = timed("new", 1792720740 + 1, 1792720740 + 86400)
        self.assertEqual(checkin.ordered_claimable([expired, future], now), [])
        self.assertFalse(checkin.has_claimed_today([expired], now))

    def test_has_claimed_today_ignores_previous_windows(self):
        now = utc(2026, 10, 23, 3, 0)  # 北京时间 11:00，当前窗口 act-20261023
        items = [
            campaign("old", status="CLAIMED", key="act-20261022-001"),
            campaign("view", action="VIEW_DETAILS", key="act-20261023-001"),
        ]
        self.assertFalse(checkin.has_claimed_today(items, now))

        items.append(campaign("today", status="CLAIMED", key="act-20261023-001"))
        self.assertTrue(checkin.has_claimed_today(items, now))

    def test_has_claimed_today_falls_back_when_key_missing(self):
        now = utc(2026, 10, 23, 3, 0)
        self.assertTrue(
            checkin.has_claimed_today([campaign("x", status="CLAIMED")], now)
        )

    def test_claim_accepts_replayed_without_grant_id(self):
        session = FakeSession(
            claim_handlers={"c1": lambda: FakeResponse(200, {"replayed": True})}
        )
        self.assertEqual(
            checkin.claim(session, "c1"), {"replayed": True}
        )

    def test_claim_rejects_response_without_grant_or_replayed(self):
        session = FakeSession(claim_handlers={"c1": lambda: FakeResponse(200, {})})
        with self.assertRaises(checkin.QoderError) as ctx:
            checkin.claim(session, "c1")
        self.assertEqual(ctx.exception.type, "schema_changed")

    def test_ssl_error_is_classified_as_network_with_hint(self):
        import requests

        class TlsSession:
            def request(self, *args, **kwargs):
                raise requests.exceptions.SSLError("certificate verify failed")

        with self.assertRaises(checkin.QoderError) as ctx:
            checkin.request_json(TlsSession(), "POST", checkin.PATH_EXCHANGE, where="jobToken 交换")
        self.assertEqual(ctx.exception.type, "network")
        self.assertIn("TLS 握手失败", ctx.exception.message)
        self.assertIn("truststore", ctx.exception.message)

    def test_mask_never_leaks_token(self):
        token = "pt-1234567890abcdef"
        masked = checkin.mask(token)
        self.assertNotIn(token, masked)
        self.assertEqual(masked, "pt-123***cdef")
        self.assertEqual(checkin.mask(""), "(空)")


class TestRunAccount(unittest.TestCase):
    def run_with(self, session):
        original = checkin.make_session
        checkin.make_session = lambda job_token=None: session
        self.addCleanup(setattr, checkin, "make_session", original)
        return checkin.run_account("pt-123456789012345", 1)

    def test_partial_failure_still_claims_remaining(self):
        session = FakeSession(
            campaigns=[campaign("a"), campaign("b")],
            claim_handlers={
                "a": lambda: FakeResponse(500, {}),
                "b": lambda: FakeResponse(200, {"grantId": "g-b", "benefit": {"amount": 100}}),
            },
        )
        result = self.run_with(session)
        self.assertEqual(len(session.claim_calls()), 2)
        self.assertFalse(result["ok"])
        self.assertIn("部分签到成功", result["message"])
        self.assertIn("+100 CREDITS", result["message"])

    def test_all_claims_fail(self):
        session = FakeSession(
            campaigns=[campaign("a")],
            claim_handlers={"a": lambda: FakeResponse(500, {})},
        )
        result = self.run_with(session)
        self.assertFalse(result["ok"])
        self.assertIn("签到失败", result["message"])

    def test_no_active_campaign_is_failure(self):
        result = self.run_with(FakeSession(campaigns=[]))
        self.assertFalse(result["ok"])
        self.assertIn("没有可领取", result["message"])

    def test_already_claimed_is_success(self):
        session = FakeSession(campaigns=[campaign("c", status="CLAIMED")])
        result = self.run_with(session)
        self.assertTrue(result["ok"])
        self.assertIn("今天已签到", result["message"])

    def test_replayed_only_reports_no_new_credits(self):
        session = FakeSession(
            campaigns=[campaign("a", amount=100)],
            claim_handlers={"a": lambda: FakeResponse(200, {"replayed": True})},
        )
        result = self.run_with(session)
        self.assertTrue(result["ok"])
        self.assertIn("本次无新增额度", result["message"])
        self.assertNotIn("+100", result["message"])

    def test_auth_failure_aborts_remaining_claims(self):
        session = FakeSession(
            campaigns=[campaign("a"), campaign("b")],
            claim_handlers={"a": lambda: FakeResponse(401, {}), "b": lambda: FakeResponse(200, {"grantId": "g"})},
        )
        result = self.run_with(session)
        self.assertEqual(len(session.claim_calls()), 1)
        self.assertFalse(result["ok"])
        self.assertIn("PAT 已失效", result["message"])

    def test_usage_auth_failure_marks_account_failed(self):
        session = FakeSession(campaigns=[campaign("a")])
        original = session.request

        def request(method, url, **kwargs):
            if url.endswith(checkin.PATH_USAGE):
                session.calls.append((method, checkin.PATH_USAGE))
                return FakeResponse(401, {})
            return original(method, url, **kwargs)

        session.request = request
        result = self.run_with(session)
        self.assertFalse(result["ok"])
        self.assertIn("余额查询阶段", result["message"])

    def test_usage_failure_does_not_fail_successful_claim(self):
        session = FakeSession(
            campaigns=[campaign("a")],
            usage={"qoderUsage": {"userQuota": {"total": 10, "remaining": 5}}},
        )
        original = session.request

        def request(method, url, **kwargs):
            if url.endswith(checkin.PATH_USAGE):
                session.calls.append((method, checkin.PATH_USAGE))
                return FakeResponse(500, {})
            return original(method, url, **kwargs)

        session.request = request
        result = self.run_with(session)
        self.assertTrue(result["ok"])
        self.assertIn("余额查询失败", result["balance"])


if __name__ == "__main__":
    unittest.main()
