import json
from pathlib import Path
import tempfile
import unittest

from core.profiles import build_profile, default_profile, ProfileError, ProfileStore, validate_profile


class ProfilesTests(unittest.TestCase):
    def test_chinese_conditions_do_not_invent_missing_facts(self):
        profile = build_profile({"conditions": "店铺：小林杂货\n商品：蓝色马克杯\n商品ID：cup-1\n售价：￥39.90元\n最低价：35\n发货：现货下单后两天内发出\n不要承诺赠品"})
        product = profile["products"][0]
        self.assertEqual(product["id"], "cup-1")
        self.assertEqual(product["price"], 39.9)
        self.assertEqual(product["min_price"], 35.0)
        self.assertIsNone(product["stock"])
        self.assertEqual(product["description"], "")
        self.assertIn("不要承诺赠品", profile["custom_instructions"])

    def test_explicit_fields_override_conditions(self):
        profile = build_profile({"conditions": "商品：杯子\n价格：50", "price": 40, "product_id": "specific"})
        self.assertEqual(profile["products"][0]["price"], 40)
        self.assertEqual(profile["products"][0]["id"], "specific")

    def test_multi_product_prose_is_rejected_as_ambiguous(self):
        with self.assertRaises(ProfileError):
            build_profile({"products": [{"id": "x", "name": "杯子"}], "conditions": "售价：10"})

    def test_invalid_money_values_are_rejected(self):
        for value in (True, -1, "nan", "inf", 1.001, "abc", 1000000000):
            with self.subTest(value=value), self.assertRaises(ProfileError):
                build_profile({"product_name": "杯子", "price": value})

    def test_minimum_cannot_exceed_price(self):
        for fields in ({"price": 10, "min_price": 11}, {"min_price": 10}):
            with self.subTest(fields=fields), self.assertRaises(ProfileError):
                build_profile({"product_name": "杯子", **fields})

    def test_stock_is_integer_or_unknown(self):
        self.assertIsNone(build_profile({"product_name": "杯子", "stock": ""})["products"][0]["stock"])
        self.assertEqual(build_profile({"product_name": "杯子", "stock": "3"})["products"][0]["stock"], 3)
        for value in (-1, True, 3.5, "不多"):
            with self.subTest(value=value), self.assertRaises(ProfileError):
                build_profile({"product_name": "杯子", "stock": value})

    def test_duplicate_product_id_rejected(self):
        with self.assertRaises(ProfileError):
            validate_profile({"products": [{"id": "x", "name": "甲"}, {"id": "x", "name": "乙"}]})

    def test_platform_scoped_ids_and_chinese_condition_names(self):
        p = build_profile({"products": [
            {"id": "123", "name": "通用商品"},
            {"id": "123", "name": "闲鱼商品", "platform": "闲鱼"},
            {"id": "123", "name": "淘宝商品", "platform": "taobao"},
        ]})
        self.assertEqual([item["platform"] for item in p["products"]], ["", "xianyu", "taobao"])
        for label, expected in (("闲鱼", "xianyu"), ("淘宝", "taobao"), ("京东", "jingdong")):
            p = build_profile({"product_name": "杯子", "conditions": f"平台：{label}"})
            self.assertEqual(p["products"][0]["platform"], expected)
        with self.assertRaises(ProfileError):
            build_profile({"products": [{"id": "123", "name": "甲", "platform": "闲鱼"}, {"id": "123", "name": "乙", "platform": "xianyu"}]})
        with self.assertRaises(ProfileError):
            build_profile({"product_name": "杯子", "platform": "xiaoyu-typo"})

    def test_new_platform_names_parse_from_form_and_conditions(self):
        platforms = (("拼多多", "pinduoduo"), ("抖音", "douyin"),
                     ("快手", "kuaishou"), ("小红书", "xiaohongshu"))
        for label, platform in platforms:
            with self.subTest(platform=platform):
                by_form = build_profile({"product_name": "杯子", "platform": label})
                by_conditions = build_profile({"product_name": "杯子", "conditions": f"平台：{label}"})
                by_key = build_profile({"product_name": "杯子", "platform": platform})
                self.assertEqual(by_form["products"][0]["platform"], platform)
                self.assertEqual(by_conditions["products"][0]["platform"], platform)
                self.assertEqual(by_key["products"][0]["platform"], platform)

    def test_new_platform_duplicate_validation_uses_canonical_name(self):
        platforms = (("拼多多", "pinduoduo"), ("抖音", "douyin"),
                     ("快手", "kuaishou"), ("小红书", "xiaohongshu"))
        for label, platform in platforms:
            with self.subTest(platform=platform), self.assertRaises(ProfileError):
                build_profile({"products": [
                    {"id": "123", "name": "甲", "platform": label},
                    {"id": "123", "name": "乙", "platform": platform},
                ]})
        profile = build_profile({"products": [
            {"id": "123", "name": f"{label}商品", "platform": platform}
            for label, platform in platforms
        ]})
        self.assertEqual(len(profile["products"]), 4)

    def test_unknown_platform_error_lists_new_choices(self):
        with self.assertRaises(ProfileError) as error:
            build_profile({"product_name": "杯子", "platform": "unknown"})
        for label in ("拼多多", "抖音", "快手", "小红书"):
            self.assertIn(label, str(error.exception))

    def test_faq_and_keywords_validated(self):
        p = build_profile({"product_name": "杯子", "keywords": "杯子,水杯，茶杯", "faq": [{"question": "能放洗碗机吗", "answer": "可以"}]})
        self.assertEqual(p["products"][0]["keywords"], ["杯子", "水杯", "茶杯"])
        with self.assertRaises(ProfileError):
            build_profile({"product_name": "杯子", "faq": [{"question": "问题"}]})

    def test_store_roundtrip_and_invalid_save_preserves_old(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "nested" / "profile.json")
            with self.assertRaises(FileNotFoundError):
                store.load()
            saved = store.save(build_profile({"product_name": "杯子", "price": 10}))
            self.assertEqual(store.load(), saved)
            with self.assertRaises(ProfileError):
                store.save({"products": [{"id": "x", "name": "坏配置", "price": -1}]})
            self.assertEqual(store.load(), saved)
            self.assertEqual(list(store.path.parent.glob(".profile-*")), [])

    def test_no_bargain_condition_overrides_configured_floor(self):
        for condition in ("禁止议价", "不议价", "不接受议价", "议价：否"):
            with self.subTest(condition=condition):
                p = build_profile({"product_name": "杯子", "price": 10, "min_price": 5, "bargain_enabled": True, "conditions": condition})
                self.assertFalse(p["products"][0]["bargain_enabled"])
        p = build_profile({"products": [{"id": "1", "name": "甲"}, {"id": "2", "name": "乙"}], "conditions": "禁止议价"})
        self.assertTrue(all(not product["bargain_enabled"] for product in p["products"]))

    def test_boolean_strings_rejected_to_prevent_false_becoming_true(self):
        with self.assertRaises(ProfileError):
            build_profile({"product_name": "杯子", "bargain_enabled": "false"})

    def test_default_profile_has_no_example_product_facts_and_is_independent(self):
        first = default_profile()
        self.assertEqual(first["reply_mode"], "default")
        self.assertEqual(first["products"], [])
        self.assertEqual(first["merchant_name"], "我的店铺")
        self.assertNotIn("demo-notebook", json.dumps(first))
        self.assertNotIn("示例", json.dumps(first, ensure_ascii=False))
        first["products"].append({"id": "mutated"})
        first["handoff_keywords"].clear()
        self.assertEqual(default_profile()["products"], [])
        self.assertTrue(default_profile()["handoff_keywords"])

    def test_legacy_profile_without_reply_mode_remains_custom(self):
        legacy = {"merchant_name": "已有店铺", "products": [{"id": "123", "name": "杯子", "price": 99}]}
        self.assertEqual(validate_profile(legacy)["reply_mode"], "custom")
        self.assertEqual(build_profile(legacy)["reply_mode"], "custom")
        self.assertNotIn("reply_mode", legacy)

    def test_reply_mode_validation_rejects_typos_and_non_strings(self):
        for mode in ("", "automatic", "DEFAULT", False, None, [], {}):
            with self.subTest(mode=mode), self.assertRaises(ProfileError):
                validate_profile({"reply_mode": mode})
        for mode in ("default", "custom"):
            with self.subTest(mode=mode):
                self.assertEqual(build_profile({"reply_mode": mode})["reply_mode"], mode)

    def test_reply_mode_roundtrip_preserves_retained_custom_products(self):
        original = build_profile({"reply_mode": "default", "product_name": "保留的杯子", "price": 99})
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "merchant.json")
            store.save(original)
            loaded = store.load()
            self.assertEqual(loaded["reply_mode"], "default")
            self.assertEqual(loaded["products"], original["products"])
            loaded["reply_mode"] = "custom"
            store.save(loaded)
            self.assertEqual(store.load()["products"], original["products"])
            self.assertEqual(store.load()["reply_mode"], "custom")

    def test_empty_profile_has_no_fictional_products(self):
        self.assertEqual(build_profile({})["products"], [])


if __name__ == "__main__":
    unittest.main()
