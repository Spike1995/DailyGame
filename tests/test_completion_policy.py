"""completion_policy（limbus 活动超时策略解析）单元测试。

覆盖 T-003 §6「配置兼容」行在解析器层的情形：显式启用、单键补默认、
双键自定义、错层级拒绝、非法值拒绝与 idle>hard 拒绝。纯内存，无 I/O。
"""

from __future__ import annotations

import dataclasses
import unittest

from daily_games.completion_policy import (
    CODE_IDLE_EXCEEDS_HARD,
    CODE_INVALID_VALUE,
    CODE_TIMEOUTS_NOT_MAPPING,
    CODE_UNSUPPORTED_LOCATION,
    DEFAULT_COMPLETION_HARD_S,
    DEFAULT_COMPLETION_IDLE_S,
    ActivityPolicy,
    CompletionPolicyError,
    LegacyPolicy,
    resolve_limbus_activity_policy,
)


def _config_with_limbus_timeouts(timeouts: object) -> dict:
    return {"games": {"limbus": {"timeouts": timeouts}}}


class ResolveLegacyTests(unittest.TestCase):
    def test_empty_config_is_legacy(self):
        self.assertEqual(resolve_limbus_activity_policy({}), LegacyPolicy())

    def test_missing_timeouts_is_legacy(self):
        config = {"games": {"limbus": {"display_name": "边狱巴士"}}}
        self.assertEqual(resolve_limbus_activity_policy(config), LegacyPolicy())

    def test_null_timeouts_is_legacy(self):
        self.assertEqual(
            resolve_limbus_activity_policy(_config_with_limbus_timeouts(None)),
            LegacyPolicy(),
        )

    def test_only_legacy_completion_s_is_legacy(self):
        config = _config_with_limbus_timeouts({"completion_s": 10800})
        self.assertEqual(resolve_limbus_activity_policy(config), LegacyPolicy())

    def test_global_completion_s_only_is_legacy(self):
        config = {"timeouts": {"completion_s": 7200}}
        self.assertEqual(resolve_limbus_activity_policy(config), LegacyPolicy())


class ResolveActivityTests(unittest.TestCase):
    def test_idle_only_fills_hard_default(self):
        policy = resolve_limbus_activity_policy(
            _config_with_limbus_timeouts({"completion_idle_s": 600}),
        )
        self.assertEqual(
            policy,
            ActivityPolicy(idle_s=600.0, hard_s=DEFAULT_COMPLETION_HARD_S),
        )

    def test_hard_only_fills_idle_default(self):
        policy = resolve_limbus_activity_policy(
            _config_with_limbus_timeouts({"completion_hard_s": 3600}),
        )
        self.assertEqual(
            policy,
            ActivityPolicy(idle_s=DEFAULT_COMPLETION_IDLE_S, hard_s=3600.0),
        )

    def test_both_custom_values(self):
        policy = resolve_limbus_activity_policy(
            _config_with_limbus_timeouts(
                {"completion_idle_s": 1800, "completion_hard_s": 14400},
            )
        )
        self.assertEqual(policy, ActivityPolicy(idle_s=1800.0, hard_s=14400.0))

    def test_int_values_accepted_as_float(self):
        policy = resolve_limbus_activity_policy(
            _config_with_limbus_timeouts(
                {"completion_idle_s": 90, "completion_hard_s": 100},
            )
        )
        self.assertIsInstance(policy.idle_s, float)
        self.assertIsInstance(policy.hard_s, float)

    def test_idle_equal_to_hard_is_allowed(self):
        policy = resolve_limbus_activity_policy(
            _config_with_limbus_timeouts(
                {"completion_idle_s": 60, "completion_hard_s": 60},
            )
        )
        self.assertEqual(policy, ActivityPolicy(idle_s=60.0, hard_s=60.0))

    def test_activity_keys_alongside_legacy_completion_s(self):
        policy = resolve_limbus_activity_policy(
            _config_with_limbus_timeouts(
                {"completion_s": 10800, "completion_idle_s": 2700},
            )
        )
        self.assertEqual(
            policy,
            ActivityPolicy(idle_s=2700.0, hard_s=DEFAULT_COMPLETION_HARD_S),
        )

    def test_policies_are_frozen(self):
        policy = resolve_limbus_activity_policy(
            _config_with_limbus_timeouts({"completion_idle_s": 60}),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.idle_s = 1.0  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            LegacyPolicy().mode = "activity"  # type: ignore[misc]


class ResolveInvalidValueTests(unittest.TestCase):
    def test_invalid_values_rejected(self):
        cases = [
            ("bool_true", True),
            ("bool_false", False),
            ("null", None),
            ("string", "300"),
            ("zero", 0),
            ("zero_float", 0.0),
            ("negative", -1),
            ("negative_float", -0.5),
            ("nan", float("nan")),
            ("inf", float("inf")),
            ("neg_inf", float("-inf")),
            ("list", [600]),
            ("mapping", {"idle": 600}),
        ]
        for label, value in cases:
            with self.subTest(case=label):
                config = _config_with_limbus_timeouts({"completion_idle_s": value})
                with self.assertRaises(CompletionPolicyError) as ctx:
                    resolve_limbus_activity_policy(config)
                self.assertEqual(ctx.exception.code, CODE_INVALID_VALUE)

    def test_hard_value_invalid_rejected(self):
        config = _config_with_limbus_timeouts({"completion_hard_s": "abc"})
        with self.assertRaises(CompletionPolicyError) as ctx:
            resolve_limbus_activity_policy(config)
        self.assertEqual(ctx.exception.code, CODE_INVALID_VALUE)

    def test_unrepresentable_integer_rejected(self):
        # R6：超出双精度可表示范围的整数必须归入 invalid_value，
        # 不能外泄 OverflowError（preflight 只捕获 CompletionPolicyError）。
        huge = 10 ** 400
        for key in ("completion_idle_s", "completion_hard_s"):
            with self.subTest(key=key):
                config = _config_with_limbus_timeouts({key: huge})
                with self.assertRaises(CompletionPolicyError) as ctx:
                    resolve_limbus_activity_policy(config)
                self.assertEqual(ctx.exception.code, CODE_INVALID_VALUE)

    def test_huge_integer_beyond_repr_limit_rejected(self):
        # 连 repr 都失败的量级也要稳定成配置错误（错误消息不得再抛异常）。
        config = _config_with_limbus_timeouts({"completion_hard_s": 10 ** 5000})
        with self.assertRaises(CompletionPolicyError) as ctx:
            resolve_limbus_activity_policy(config)
        self.assertEqual(ctx.exception.code, CODE_INVALID_VALUE)

    def test_idle_greater_than_hard_rejected(self):
        config = _config_with_limbus_timeouts(
            {"completion_idle_s": 7200, "completion_hard_s": 3600},
        )
        with self.assertRaises(CompletionPolicyError) as ctx:
            resolve_limbus_activity_policy(config)
        self.assertEqual(ctx.exception.code, CODE_IDLE_EXCEEDS_HARD)

    def test_timeouts_non_mapping_rejected(self):
        for label, timeouts in (("string", "abc"), ("list", [1, 2])):
            with self.subTest(case=label):
                config = _config_with_limbus_timeouts(timeouts)
                with self.assertRaises(CompletionPolicyError) as ctx:
                    resolve_limbus_activity_policy(config)
                self.assertEqual(ctx.exception.code, CODE_TIMEOUTS_NOT_MAPPING)


class ResolveLocationTests(unittest.TestCase):
    def test_global_timeouts_location_rejected(self):
        config = {"timeouts": {"completion_idle_s": 600}}
        with self.assertRaises(CompletionPolicyError) as ctx:
            resolve_limbus_activity_policy(config)
        self.assertEqual(ctx.exception.code, CODE_UNSUPPORTED_LOCATION)

    def test_other_game_timeouts_location_rejected(self):
        for game_id in ("arknights", "endfield"):
            with self.subTest(game=game_id):
                config = {
                    "games": {
                        game_id: {"timeouts": {"completion_hard_s": 3600}},
                        "limbus": {"timeouts": {"completion_idle_s": 600}},
                    },
                }
                with self.assertRaises(CompletionPolicyError) as ctx:
                    resolve_limbus_activity_policy(config)
                self.assertEqual(ctx.exception.code, CODE_UNSUPPORTED_LOCATION)

    def test_other_game_key_rejected_even_without_limbus_keys(self):
        config = {"games": {"arknights": {"timeouts": {"completion_idle_s": 1}}}}
        with self.assertRaises(CompletionPolicyError) as ctx:
            resolve_limbus_activity_policy(config)
        self.assertEqual(ctx.exception.code, CODE_UNSUPPORTED_LOCATION)


if __name__ == "__main__":
    unittest.main()
