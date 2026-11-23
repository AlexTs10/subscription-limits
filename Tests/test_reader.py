import base64
import datetime as dt
import email.utils
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("reader", Path(__file__).parents[1] / "Sources/limits_reader.py")
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class ReaderTests(unittest.TestCase):
    def test_retry_after_supports_seconds_dates_and_invalid_values(self):
        now = time.time()
        date = email.utils.format_datetime(dt.datetime.fromtimestamp(now + 7200, dt.timezone.utc))
        with patch.object(reader.time, "time", return_value=now):
            self.assertEqual(reader.retry_delay("7200"), 7200)
            self.assertAlmostEqual(reader.retry_delay(date), 7200, delta=1)
            for value in (None, "invalid", "nan", "inf", "0", "-1"):
                self.assertEqual(reader.retry_delay(value), 300)

    def test_http_429_keeps_full_retry_after(self):
        headers = Message()
        headers["Retry-After"] = "7200"
        error = urllib.error.HTTPError("https://api.anthropic.com/api/oauth/usage", 429, "", headers, None)
        with patch.object(reader.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(reader.ReadError) as raised:
                reader.get_json("https://api.anthropic.com/api/oauth/usage", "secret")
        self.assertEqual(raised.exception.retry_after, 7200)
        self.assertEqual(raised.exception.code, "rate_limited")

    def test_full_wait_survives_refreshes_and_does_not_grow_without_a_request(self):
        now = time.time()
        source = dict(identity="claude:a:o", sourceId="local", label="Claude", provider="claude", token="secret")
        previous = {"claude:a:o": dict(id="claude:a:o", limits=[dict(id="seven_day", remaining=23)], fetchedAt="old")}
        with patch.object(reader.time, "time", return_value=now), \
             patch.object(reader, "get_json", side_effect=reader.ReadError("Usage checks paused", 7200, "rate_limited")), \
             patch.object(reader, "web_fallback", side_effect=reader.ReadError("Browser reader unavailable")):
            row = reader.read_usage(source, previous)
        self.assertEqual(row["retryAt"], now + 7200)
        self.assertEqual(row["failures"], 1)
        with patch.object(reader.time, "time", return_value=now + 600), \
             patch.object(reader, "get_json") as request, patch.object(reader, "web_fallback") as fallback:
            self.assertEqual(reader.read_usage(source, {row["id"]: row}), row)
            request.assert_not_called()
            fallback.assert_not_called()

    def test_legacy_api_wait_is_preserved_and_browser_failure_is_visible(self):
        now = time.time()
        source = dict(identity="claude:a:o", sourceId="local", label="Claude", provider="claude", token="secret")
        previous = {"claude:a:o": dict(id="claude:a:o", limits=[], apiRetryAt=now + 2000, retryAt=now - 1)}
        with patch.object(reader.time, "time", return_value=now), patch.object(reader, "get_json") as request, \
             patch.object(reader, "web_fallback", side_effect=reader.ReadError("Browser reader unavailable")):
            row = reader.read_usage(source, previous)
        request.assert_not_called()
        self.assertEqual(row["retryAt"], now + 2000)
        self.assertEqual(row["fallbackError"], "Browser reader unavailable")
        self.assertEqual(row["errorCode"], "rate_limited")

    def test_repeated_rate_limits_back_off_and_success_clears_wait(self):
        source = dict(identity="claude:a:o", sourceId="local", label="Claude", provider="claude", token="secret")
        row = dict(id="claude:a:o", limits=[])
        now = time.time()
        for delay in (300, 600, 1200, 2400, 3600):
            with patch.object(reader.time, "time", return_value=now), \
                 patch.object(reader, "get_json", side_effect=reader.ReadError("Usage checks paused", 300, "rate_limited")), \
                 patch.object(reader, "web_fallback", side_effect=reader.ReadError("Browser reader unavailable")):
                row = reader.read_usage(source, {row["id"]: row})
            self.assertEqual(row["retryAt"], now + delay)
            now = row["retryAt"] + 1
        with patch.object(reader.time, "time", return_value=now), \
             patch.object(reader, "get_json", return_value={"seven_day": {"utilization": 30}}):
            row = reader.read_usage(source, {row["id"]: row})
        self.assertIsNone(row["error"])
        self.assertNotIn("apiRetryAt", row)
        self.assertNotIn("failures", row)
        self.assertEqual(row["limits"][1]["remaining"], 70)

    def test_identity_cache_is_bound_to_token_and_rechecks_expected_account(self):
        profile = {"account": {"uuid": "user", "email": "user@example.com"}, "organization": {"uuid": "org", "name": "Acme"}}
        cache = {}
        source = dict(provider="claude", token="old-secret")
        with patch.object(reader, "get_json", return_value=profile) as request:
            reader.claude_identity(dict(source), cache)
            reader.claude_identity(dict(source), cache)
            self.assertEqual(request.call_count, 1)
            with self.assertRaises(reader.ReadError):
                reader.claude_identity(dict(source, expectedOrg="different"), cache)
            with self.assertRaises(reader.ReadError):
                reader.claude_identity(dict(source, expectedAccount="different"), cache)
            self.assertEqual(request.call_count, 1)
            reader.claude_identity(dict(source, token="new-secret"), cache)
            self.assertEqual(request.call_count, 2)
        for token in ("old-secret", "new-secret"):
            self.assertNotIn(token, json.dumps(cache))

    def test_identity_cache_expires_and_honors_profile_retry(self):
        profile = {"account": {"uuid": "user"}, "organization": {"uuid": "org"}}
        cache = {}
        source = dict(provider="claude", token="secret")
        now = time.time()
        with patch.object(reader.time, "time", return_value=now), patch.object(reader, "get_json", return_value=profile):
            reader.claude_identity(dict(source), cache)
        with patch.object(reader.time, "time", return_value=now + reader.IDENTITY_TTL + 1), \
             patch.object(reader, "get_json", side_effect=reader.ReadError("Usage checks paused", 7200, "rate_limited")) as request:
            for _ in range(2):
                with self.assertRaises(reader.ReadError):
                    reader.claude_identity(dict(source), cache)
            self.assertEqual(request.call_count, 1)

    def test_snapshot_restart_reuses_verified_identity_and_recent_usage(self):
        source = dict(provider="claude", sourceId="local", label="Claude", token="secret")
        def response(url, *args):
            if url.endswith("/profile"):
                return {"account": {"uuid": "user"}, "organization": {"uuid": "org", "name": "Acme"}}
            return {"seven_day": {"utilization": 30}}
        with tempfile.TemporaryDirectory() as directory, patch.object(reader, "STATE", Path(directory)), \
             patch.object(reader, "collect_sources", side_effect=lambda: ([dict(source)], [])), \
             patch.object(reader, "get_json", side_effect=response) as request:
            first = reader.snapshot()
            second = reader.snapshot()
            self.assertEqual(request.call_count, 2)
            self.assertEqual(first["accounts"], second["accounts"])
            for path in Path(directory).glob("*.json"):
                self.assertNotIn("secret", path.read_text())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_all_subscriptions_are_separate_but_duplicate_sources_merge(self):
        identities = {
            "acme-secret": ("user-1", "org-acme", "Acme", "work@example.com"),
            "team-secret": ("user-2", "org-team", "Team", "personal@example.com"),
            "desktop-team-secret": ("user-2", "org-team", "Team", "personal@example.com"),
            "personal-secret": ("user-2", "org-personal", "Personal", "personal@example.com"),
        }
        sources = [dict(provider="claude", sourceId="source-" + str(i), label="Claude", token=token) for i, token in enumerate(identities)]
        sources.append(dict(provider="codex", sourceId="codex-home", label="Codex", token="codex-secret", accountId="codex-account"))

        def response(url, token, account_id=None):
            if url.endswith("/profile"):
                user, org, name, email = identities[token]
                return {"account": {"uuid": user, "email": email}, "organization": {"uuid": org, "name": name}}
            if "wham" in url:
                return {"rate_limit": {"primary_window": {"limit_window_seconds": 604800, "used_percent": 57, "reset_at": 1800000000}}}
            return {"five_hour": {"utilization": 27, "resets_at": None}, "seven_day": {"utilization": 97 if token == "personal-secret" else 8}}

        with tempfile.TemporaryDirectory() as directory, patch.object(reader, "STATE", Path(directory)), \
             patch.object(reader, "collect_sources", return_value=(sources, [])), patch.object(reader, "get_json", side_effect=response):
            result = reader.snapshot()
            self.assertEqual(len(result["accounts"]), 4)
            team = next(r for r in result["accounts"] if r["id"].endswith(":org-team"))
            personal = next(r for r in result["accounts"] if r["id"].endswith(":org-personal"))
            self.assertEqual(len(team["sourceIds"]), 2)
            self.assertEqual(personal["limits"][1]["remaining"], 3)
            codex = next(r for r in result["accounts"] if r["provider"] == "codex")
            self.assertIsNone(codex["limits"][0]["remaining"])
            self.assertEqual(codex["limits"][1]["remaining"], 43)
            persisted = (Path(directory) / "snapshot.json").read_text()
            for forbidden in ('"token"', '"auth"', '"refreshToken"', '"accessToken"', 'codex-secret'):
                self.assertNotIn(forbidden, persisted)
            for token in identities:
                self.assertNotIn(token, persisted)
            self.assertEqual((Path(directory) / "snapshot.json").stat().st_mode & 0o777, 0o600)

    def test_rate_limit_keeps_old_values_and_honors_retry_delay(self):
        source = dict(identity="claude:a:o", sourceId="local", label="Claude", provider="claude", token="secret")
        previous = {"claude:a:o": dict(id="claude:a:o", limits=[dict(id="seven_day", remaining=23)], fetchedAt="2026-09-24T12:00:00Z")}
        with patch.object(reader, "get_json", side_effect=reader.ReadError("Provider busy", 300)):
            row = reader.read_usage(source, previous)
        self.assertEqual(row["limits"][0]["remaining"], 23)
        self.assertEqual(row["fetchedAt"], previous["claude:a:o"]["fetchedAt"])
        self.assertGreater(row["retryAt"], time.time() + 290)
        with patch.object(reader, "get_json") as request:
            self.assertEqual(reader.read_usage(source, {row["id"]: row}), row)
            request.assert_not_called()

    def test_desktop_wrong_organization_is_rejected_before_usage(self):
        source = dict(provider="claude", token="secret", expectedOrg="expected")
        with patch.object(reader, "get_json", return_value={"account": {"uuid": "user"}, "organization": {"uuid": "different"}}):
            with self.assertRaises(reader.ReadError):
                reader.claude_identity(source)

    def test_malformed_response_does_not_erase_previous_values(self):
        source = dict(identity="codex:one", sourceId="local", label="Codex", provider="codex", token="secret")
        previous = {"codex:one": dict(limits=[dict(id="seven_day", remaining=43)], fetchedAt="old")}
        with patch.object(reader, "get_json", return_value={"rate_limit": []}):
            # An empty provider object means unknown, not 100% remaining.
            self.assertTrue(all(r["remaining"] is None for r in reader.read_usage(source, previous)["limits"]))
        with patch.object(reader, "get_json", return_value={"rate_limit": ["bad"]}):
            row = reader.read_usage(source, previous)
            self.assertEqual(row["limits"][0]["remaining"], 43)
            self.assertEqual(row["error"], "Provider response format changed")

    def test_invalid_percent_is_unknown(self):
        for value in (None, True, -1, float("inf"), float("nan"), "50"):
            self.assertIsNone(reader.remaining(value))
        self.assertEqual(reader.remaining(100), 0)
        self.assertEqual(reader.remaining(0), 100)

    def test_antigravity_keychain_envelope_only_accepts_access_tokens(self):
        envelope = "go-keyring-base64:" + base64.b64encode(json.dumps({"access_token": "secret"}).encode()).decode()
        self.assertEqual(reader.google_access_token(envelope), "secret")
        self.assertEqual(reader.google_access_token(json.dumps({"refresh_token": "not-an-access-token"})), None)
        self.assertEqual(reader.google_access_token("not-json"), None)

    def test_antigravity_cli_login_keeps_its_id_when_the_token_changes(self):
        def found(token):
            login = {"token": {"access_token": token, "refresh_token": "r"}, "id_token_email": "me@example.com"}
            stored = b"go-keyring-base64:" + base64.b64encode(json.dumps(login).encode())
            with patch.object(reader, "sqlite_value", return_value=None), \
                 patch.object(reader, "load_json", return_value={}), \
                 patch.object(reader, "keychain", return_value=stored):
                return reader.antigravity_tokens()
        first, second = found("one"), found("two")
        self.assertEqual((first[0][1], second[0][1]), ("one", "two"))
        self.assertEqual(first[0][2:], second[0][2:])
        self.assertEqual(first[0][3], "me@example.com")

    def test_antigravity_quota_uses_reported_bucket_fractions_and_resets(self):
        data = {"groups": [
            {"displayName": "Gemini Models", "buckets": [
                {"bucketId": "gemini-5h", "remaining": {"remainingFraction": 0.82}, "resetTime": "2026-11-23T12:00:00Z"},
                {"bucketId": "gemini-weekly", "remainingFraction": 0.35, "resetTime": "2026-11-29T12:00:00Z"},
            ]},
            {"displayName": "Claude and GPT models", "buckets": [
                {"bucketId": "non-gemini-5h", "remainingFraction": 0.1},
            ]},
        ]}
        rows = reader.antigravity_windows(data)
        self.assertEqual([(row["name"], row["remaining"]) for row in rows], [
            ("Gemini 5-hour", 82), ("Gemini weekly", 35), ("Claude/GPT 5-hour", 10),
        ])
        self.assertEqual(rows[0]["resetsAt"], "2026-11-23T12:00:00Z")

    def test_antigravity_unknown_or_missing_buckets_are_not_reported_as_full(self):
        with self.assertRaises(ValueError):
            reader.antigravity_windows({"groups": [{"buckets": [{"bucketId": "gemini-5h", "remainingFraction": None}]}]})

    def test_antigravity_reads_quota_after_loading_its_project(self):
        source = dict(provider="antigravity", sourceId="antigravity:one", label="Gemini / Antigravity", token="secret")
        summary = {"groups": [{"displayName": "Gemini", "buckets": [
            {"bucketId": "gemini-5h", "remainingFraction": 0.4},
        ]}]}
        with patch.object(reader, "post_json", side_effect=[{"cloudaicompanionProject": "project-1"}, summary]) as request:
            row = reader.read_usage(source, {})
        self.assertEqual(row["limits"][0]["remaining"], 40)
        self.assertIsNone(row["error"])
        # Google rejects any field it does not know with HTTP 400.
        self.assertEqual([call.args[2] for call in request.call_args_list],
                         [{"metadata": {"ideType": "ANTIGRAVITY"}}, {"project": "project-1"}])

    def test_browser_fallback_does_not_use_another_organization(self):
        record = {"provider": "claude", "usage": {"accountEmail": "same@example.com", "accountOrganization": "Other",
                  "primary": {"windowMinutes": 300, "usedPercent": 99}, "updatedAt": "2026-09-24T12:00:00Z"}}
        source = dict(webFallbackAllowed=True, detail="same@example.com", organizationName="Acme")
        with patch.object(reader.Path, "is_file", return_value=True), patch.object(reader.subprocess, "Popen") as popen:
            process = popen.return_value.__enter__.return_value
            process.returncode = 0
            process.communicate.return_value = (json.dumps([record]).encode(), None)
            with self.assertRaises(reader.ReadError):
                reader.web_fallback(source)
            record["usage"]["accountOrganization"] = "Acme"
            process.communicate.return_value = (json.dumps([record]).encode(), None)
            rows, fetched = reader.web_fallback(source)
            self.assertEqual(rows[0]["remaining"], 1)
            self.assertIsNone(rows[1]["remaining"])
            self.assertEqual(fetched, "2026-09-24T12:00:00Z")

    def test_ambiguous_browser_identity_is_not_used(self):
        with patch.object(reader.subprocess, "Popen") as popen:
            with self.assertRaises(reader.ReadError):
                reader.web_fallback(dict(webFallbackAllowed=False))
            popen.assert_not_called()

    def test_shared_codex_workspace_preserves_each_user(self):
        def auth(subject):
            encoded = base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode()).decode().rstrip("=")
            return {"id_token": "header." + encoded + ".signature"}
        sources = [dict(provider="codex", sourceId=user, label="Codex", token="secret", accountId="same-workspace", auth=auth(user)) for user in ("one", "two")]
        with tempfile.TemporaryDirectory() as directory, patch.object(reader, "STATE", Path(directory)), \
             patch.object(reader, "collect_sources", return_value=(sources, [])), patch.object(reader, "get_json", return_value={}):
            rows = reader.snapshot()["accounts"]
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]["id"], rows[1]["id"])


if __name__ == "__main__":
    unittest.main()
