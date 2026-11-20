#!/usr/bin/python3
"""Read subscription limits without changing provider credentials or accounts.

Only usage snapshots are saved. Access tokens stay in memory and are sent only
to their provider's HTTPS usage/profile endpoint. Token renewal belongs to the
original Claude/Codex application.
"""
import base64
import concurrent.futures
import ctypes
import datetime as dt
import email.utils
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request

HOME = Path.home()
STATE = HOME / "Library/Application Support/Subscription Limits"
CLAUDE_DESKTOP = HOME / "Library/Application Support/Claude/config.json"
T3_SETTINGS = HOME / ".t3/userdata/settings.json"
ISO = lambda: dt.datetime.now(dt.timezone.utc).isoformat()
POLL_INTERVAL = 300
IDENTITY_TTL = 86400


class ReadError(Exception):
    def __init__(self, message, retry_after=0, code=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.code = code


def retry_delay(value):
    """Accept both Retry-After formats. Never shorten the provider's wait."""
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            delay = POLL_INTERVAL
    return max(POLL_INTERVAL, delay) if math.isfinite(delay) else POLL_INTERVAL


def failure_delay(exc, failures):
    backoff = min(3600, POLL_INTERVAL * 2 ** min(max(failures - 1, 0), 4))
    return max(exc.retry_after, backoff if exc.code == "rate_limited" else POLL_INTERVAL)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".%s.tmp" % os.getpid())
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(data, handle)
    os.replace(temporary, path)


def load_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def keychain(service):
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ReadError("Keychain access unavailable")
    if result.returncode:
        raise ReadError("Login unavailable in Keychain")
    return result.stdout.rstrip(b"\r\n")


def decrypt_desktop_cache(encoded, secret):
    raw = base64.b64decode(encoded, validate=True)
    if not raw.startswith(b"v10"):
        raise ReadError("Claude Desktop storage format changed")
    key = hashlib.pbkdf2_hmac("sha1", secret, b"saltysalt", 1003, 16)
    crypto = ctypes.CDLL("/usr/lib/system/libcommonCrypto.dylib")
    crypto.CCCrypt.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    crypto.CCCrypt.restype = ctypes.c_int
    output = ctypes.create_string_buffer(len(raw))
    size = ctypes.c_size_t()
    status = crypto.CCCrypt(1, 0, 1, key, len(key), b" " * 16,
        raw[3:], len(raw) - 3, output, len(raw), ctypes.byref(size))
    if status:
        raise ReadError("Cannot read Claude Desktop login")
    return json.loads(output.raw[:size.value])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(url, token, account_id=None):
    if url not in ("https://api.anthropic.com/api/oauth/profile",
                    "https://api.anthropic.com/api/oauth/usage",
                    "https://chatgpt.com/backend-api/wham/usage"):
        raise ReadError("Unsupported provider endpoint")
    headers = {"Authorization": "Bearer " + token, "User-Agent": "SubscriptionLimits/1.0",
               "Accept": "application/json"}
    if "anthropic.com" in url:
        headers["anthropic-beta"] = "oauth-2025-04-20"
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            data = json.load(response)
            if not isinstance(data, dict):
                raise ReadError("Provider response format changed", 300)
            return data
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise ReadError("Usage checks paused (HTTP 429)",
                            retry_delay(exc.headers.get("Retry-After")), "rate_limited")
        if exc.code in (401, 403):
            raise ReadError("Open the source app to renew its login", 300)
        raise ReadError("Provider unavailable (HTTP %s)" % exc.code, 180)
    except (OSError, ValueError):
        raise ReadError("Connection unavailable", 60)


def profile_homes():
    profiles = {("claude", ""): "Claude Code", ("codex", str(HOME / ".codex")): "Codex"}
    settings = load_json(T3_SETTINGS)
    for entry in settings.get("providerInstances", {}).values():
        driver = entry.get("driver")
        provider = {"claudeAgent": "claude", "codex": "codex"}.get(driver)
        if not provider:
            continue
        config = entry.get("config") or {}
        root = config.get("homePath") or ""
        root = os.path.expanduser(root) if root else (str(HOME / ".codex") if provider == "codex" else "")
        profiles[(provider, root)] = entry.get("displayName") or provider.title()
    for p in HOME.glob(".claude*"):
        if p.is_dir() and p.name != ".claude" and load_json(p / ".claude.json").get("oauthAccount"):
            profiles.setdefault(("claude", str(p)), "Claude " + p.name.removeprefix(".claude-"))
    for p in HOME.glob(".codex*"):
        if p.is_dir() and (p / "auth.json").is_file():
            profiles.setdefault(("codex", str(p)), "Codex" if p.name == ".codex" else p.name)
    for path in (HOME / ".config/codexbar/config.json", HOME / ".codexbar/config.json"):
        for entry in load_json(path).get("providers", []):
            if entry.get("id") == "codex":
                for raw in entry.get("codexProfileHomePaths", []):
                    if isinstance(raw, str):
                        root = str(Path(raw).expanduser())
                        profiles.setdefault(("codex", root), "Codex " + Path(root).name)
    # An explicit ~/.claude and the ambient profile may have different secure
    # stores. Preserve explicit homes rather than merge accounts by directory.
    return profiles


def collect_sources():
    sources, errors = [], []
    for (provider, root), label in profile_homes().items():
        source_id = provider + ":home:" + (root or "default")
        try:
            if provider == "codex":
                data = load_json(Path(root) / "auth.json")
                token = (data.get("tokens") or {}).get("access_token")
                if not token:
                    raise ReadError("No subscription login found")
                sources.append(dict(provider=provider, sourceId=source_id, label=label,
                    token=token, accountId=(data.get("tokens") or {}).get("account_id"),
                    source="Codex Code", home=root, auth=data.get("tokens", {})))
            else:
                service = "Claude Code-credentials"
                if root:
                    service += "-" + hashlib.sha256(unicodedata.normalize("NFC", root).encode()).hexdigest()[:8]
                try:
                    data = json.loads(keychain(service))
                except ReadError:
                    data = load_json(Path(root or str(HOME / ".claude")) / ".credentials.json")
                credential = data.get("claudeAiOauth") or {}
                if not credential.get("accessToken"):
                    raise ReadError("No subscription login found")
                sources.append(dict(provider=provider, sourceId=source_id, label=label,
                    source="Claude Code", token=credential["accessToken"],
                    plan=credential.get("subscriptionType"), priority=0))
        except (ReadError, ValueError, TypeError) as exc:
            errors.append(error_row(source_id, label, provider, str(exc) if isinstance(exc, ReadError) else "Login unavailable"))
    config = load_json(CLAUDE_DESKTOP)
    encrypted = config.get("oauth:tokenCacheV2")
    if encrypted:
        try:
            cache = decrypt_desktop_cache(encrypted, keychain("Claude Safe Storage"))
            grouped = {}
            pattern = r"^(?:acct:([a-f0-9-]+)\|)?[a-f0-9-]+:([a-f0-9-]+):https://api\.anthropic\.com:(.*)$"
            for key, value in cache.items():
                match = re.match(pattern, key)
                if not match or not isinstance(value, dict) or "user:profile" not in match[3].split():
                    continue
                if not value.get("token"):
                    continue
                identity = (match[1], match[2])
                if value.get("expiresAt", 0) > grouped.get(identity, {}).get("expiresAt", -1):
                    grouped[identity] = value
            for (account, organization), value in grouped.items():
                sources.append(dict(provider="claude", sourceId="claude:desktop:" + organization,
                    label="Claude Personal" if value.get("subscriptionType") in ("max", "pro") else "Claude Desktop",
                    source="Claude Desktop", token=value["token"], expectedOrg=organization,
                    expectedAccount=account, plan=value.get("subscriptionType"), priority=1))
        except (ReadError, ValueError, TypeError, KeyError, OSError):
            errors.append(error_row("claude:desktop", "Claude Desktop", "claude", "Cannot read the Claude Desktop login"))
    return sources, errors


def error_row(identity, label, provider, message):
    return dict(id=identity, label=label, provider=provider, detail="", limits=[], error=message,
                fetchedAt=None, sourceIds=[identity])


def remaining(used):
    if isinstance(used, bool) or not isinstance(used, (float, int)) or not math.isfinite(used) or used < 0:
        return None
    return max(0, min(100, 100 - used))


def claude_windows(data):
    rows = []
    keys = [("five_hour", "5-hour"), ("seven_day", "Weekly")]
    keys += [(k, k.removeprefix("seven_day_").replace("_", " ").title() + " weekly")
             for k in data if k.startswith("seven_day_") and k not in ("seven_day_oauth_apps",)]
    for key, name in keys:
        window = data.get(key)
        if isinstance(window, dict):
            value = remaining(window.get("utilization"))
            if value is not None:
                rows.append(dict(id=key, name=name, remaining=value, resetsAt=window.get("resets_at")))
    for item in data.get("limits") or []:
        if not isinstance(item, dict):
            continue
        # New provider payloads expose explicit typed windows.
        for key, name in (("five_hour", "5-hour"), ("seven_day", "Weekly"), ("weekly_scoped", None)):
            window = item.get(key)
            if not isinstance(window, dict):
                continue
            title = name or ((window.get("scope") or window.get("name") or item.get("name") or "Model") + " weekly")
            value = remaining(window.get("utilization"))
            identity = key if name else key + ":" + title
            if value is not None and not any(r["id"] == identity for r in rows):
                rows.append(dict(id=identity, name=title, remaining=value, resetsAt=window.get("resets_at")))
    return ensure_standard_windows(rows)


def ensure_standard_windows(rows):
    # Missing data must remain unknown, never 100% remaining.
    for identity, name in (("five_hour", "5-hour"), ("seven_day", "Weekly")):
        if not any(r["id"] == identity for r in rows):
            rows.insert(0 if identity == "five_hour" else min(1, len(rows)),
                        dict(id=identity, name=name, remaining=None, resetsAt=None))
    return rows


def codex_windows(data):
    rows = []
    rate = data.get("rate_limit") or {}
    for key in ("primary_window", "secondary_window"):
        window = rate.get(key)
        if not isinstance(window, dict):
            continue
        seconds = window.get("limit_window_seconds")
        identity, name = {18000: ("five_hour", "5-hour"), 604800: ("seven_day", "Weekly")}.get(seconds, (key, "Usage"))
        stamp = window.get("reset_at")
        try:
            reset = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat() if isinstance(stamp, (int, float)) else None
        except (ValueError, OverflowError, OSError):
            reset = None
        rows.append(dict(id=identity, name=name, remaining=remaining(window.get("used_percent")), resetsAt=reset))
    return ensure_standard_windows(rows)


def claude_identity(source, cache=None):
    # Bind cached verification to the exact credential, not a profile path or
    # email. A new login or token must be verified again. Store no raw tokens.
    fingerprint = hashlib.sha256(source["token"].encode()).hexdigest()
    cached = (cache or {}).get(fingerprint, {})
    now = time.time()
    if cached.get("retryAt", 0) > now:
        raise ReadError(cached["error"], cached["retryAt"] - now, cached.get("errorCode"))
    fresh = bool(cached.get("profile") and cached.get("verifiedAt", 0) + IDENTITY_TTL > now)
    if fresh:
        profile = cached["profile"]
    else:
        try:
            profile = get_json("https://api.anthropic.com/api/oauth/profile", source["token"])
        except ReadError as exc:
            if cache is not None:
                failures = cached.get("failures", 0) + 1
                cache[fingerprint] = dict(error=str(exc), errorCode=exc.code, failures=failures,
                                          retryAt=now + failure_delay(exc, failures))
            raise
    account, organization = profile.get("account") or {}, profile.get("organization") or {}
    aid, oid = account.get("uuid"), organization.get("uuid")
    if not aid or not oid or (source.get("expectedOrg") and source["expectedOrg"] != oid):
        raise ReadError("Account identity could not be verified")
    if source.get("expectedAccount") and source["expectedAccount"] != aid:
        raise ReadError("Account identity could not be verified")
    if cache is not None and not fresh:
        cache[fingerprint] = dict(verifiedAt=now, profile=dict(
            account=dict(uuid=aid, email=account.get("email")),
            organization=dict(uuid=oid, name=organization.get("name"))))
    source["identity"] = "claude:" + aid + ":" + oid
    name = organization.get("name") or "Account"
    source["organizationName"] = name
    if source.get("plan") in ("max", "pro"):
        name = "Personal"
    source["label"] = "Claude · " + name
    source["detail"] = account.get("email") or ""
    return source


def web_fallback(source):
    """Use the installed browser reader only for an unambiguous identity match.

    Its public result supplies email and organization name, not the organization
    UUID. A match with multiple locally verified organizations is rejected.
    The native app and the other accounts do not depend on CodexBar's GUI.
    """
    if not source.get("webFallbackAllowed"):
        raise ReadError("Browser account cannot be matched safely")
    executable = next((p for p in ("/opt/homebrew/bin/codexbar", "/usr/local/bin/codexbar") if Path(p).is_file()), None)
    if not executable:
        raise ReadError("Browser reader unavailable")
    command = [executable, "usage", "--provider", "claude", "--source", "web",
               "--no-credits", "--json", "--web-timeout", "12"]
    try:
        with subprocess.Popen(command, cwd=HOME, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              start_new_session=True) as proc:
            try:
                output, _ = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                raise ReadError("Browser reader timed out")
            if proc.returncode or len(output) > 262144:
                raise ReadError("Browser usage unavailable")
        records = json.loads(output)
        if not isinstance(records, list) or len(records) != 1:
            raise ReadError("Browser usage unavailable")
        record = records[0]
        usage = record.get("usage") or {}
        identity = usage.get("identity") or {}
        email = identity.get("accountEmail") or usage.get("accountEmail")
        organization = identity.get("accountOrganization") or usage.get("accountOrganization")
        if (record.get("provider") != "claude" or record.get("error") or
            not email or email != source.get("detail") or
            not organization or organization != source.get("organizationName")):
            raise ReadError("Browser login is for a different account")
        rows = []
        windows = [(None, usage.get(k)) for k in ("primary", "secondary")]
        windows += [(r.get("title"), r.get("window")) for r in usage.get("extraRateWindows", []) if isinstance(r, dict)]
        for title, window in windows:
            if not isinstance(window, dict) or remaining(window.get("usedPercent")) is None:
                continue
            minutes = window.get("windowMinutes")
            if title and minutes == 10080:
                key, name = "scoped:" + title, title.removesuffix(" only") + " weekly"
            elif minutes in (300, 10080):
                key, name = {300: ("five_hour", "5-hour"), 10080: ("seven_day", "Weekly")}[minutes]
            else:
                continue
            rows.append(dict(id=key, name=name, remaining=remaining(window["usedPercent"]), resetsAt=window.get("resetsAt")))
        if not rows or not usage.get("updatedAt"):
            raise ReadError("Browser usage unavailable")
        return ensure_standard_windows(rows), usage["updatedAt"]
    except (OSError, ValueError, TypeError, AttributeError):
        raise ReadError("Browser usage unavailable")


def read_usage(source, old):
    identity = source.get("identity", source["sourceId"])
    row = error_row(identity, source["label"], source["provider"], "")
    row["detail"] = source.get("detail", "")
    row["sourceIds"] = source.get("sourceIds", [source["sourceId"]])
    previous = old.get(identity)
    now = time.time()
    if previous and max(previous.get("retryAt", 0), previous.get("nextFetchAt", 0)) > now:
        return previous
    try:
        if source["provider"] == "claude":
            if previous and previous.get("apiRetryAt", 0) > time.time():
                raise ReadError(previous.get("apiError", "Usage checks paused (HTTP 429)"),
                                previous["apiRetryAt"] - time.time(),
                                previous.get("apiErrorCode", "rate_limited"))
            raw = get_json("https://api.anthropic.com/api/oauth/usage", source["token"])
            row["limits"] = claude_windows(raw)
        else:
            raw = get_json("https://chatgpt.com/backend-api/wham/usage", source["token"], source.get("accountId"))
            row["limits"] = codex_windows(raw)
        row.update(fetchedAt=ISO(), error=None, retryAt=0, nextFetchAt=time.time() + POLL_INTERVAL)
    except ReadError as exc:
        # An existing wait is not a new failed request. Keep its deadline.
        waiting = bool(previous and previous.get("apiRetryAt", 0) > now)
        failures = (previous or {}).get("failures", 0) + (0 if waiting else 1)
        deadline = previous["apiRetryAt"] if waiting else time.time() + failure_delay(exc, failures)
        row.update(failures=failures, errorCode=exc.code)
        if source["provider"] == "claude":
            row.update(apiRetryAt=deadline, apiError=str(exc), apiErrorCode=exc.code)
            try:
                limits, fetched_at = web_fallback(source)
                row.update(limits=limits, fetchedAt=fetched_at, error=None, errorCode=None,
                           retryAt=0, nextFetchAt=time.time() + POLL_INTERVAL)
                return row
            except ReadError as fallback:
                row["fallbackError"] = str(fallback)
        if previous:
            row.update(limits=previous.get("limits", []), fetchedAt=previous.get("fetchedAt"))
        row.update(error=str(exc), retryAt=deadline)
    except (ValueError, TypeError, AttributeError, OverflowError):
        if previous:
            row.update(limits=previous.get("limits", []), fetchedAt=previous.get("fetchedAt"))
        row.update(error="Provider response format changed", retryAt=time.time() + 300)
    return row


def snapshot():
    previous = load_json(STATE / "snapshot.json")
    old = {r["id"]: r for r in previous.get("accounts", [])}
    sources, errors = collect_sources()
    fingerprints = {hashlib.sha256(s["token"].encode()).hexdigest()
                    for s in sources if s["provider"] == "claude"}
    identity_cache = {k: v for k, v in load_json(STATE / "identities.json").items() if k in fingerprints}
    verified, by_identity = [], {}
    for source in sources:
        try:
            if source["provider"] == "claude":
                source = claude_identity(source, identity_cache)
            else:
                subject = ""
                try:
                    encoded = source.get("auth", {}).get("id_token", "").split(".")[1]
                    claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
                    source["detail"] = claims.get("email", "")
                    subject = claims.get("sub") or ""
                except (IndexError, ValueError):
                    pass
                source["identity"] = "codex:" + (source.get("accountId") or source["sourceId"]) + (":" + subject if subject else "")
            key = source["identity"]
            if key in by_identity:
                by_identity[key]["sourceIds"].append(source["sourceId"])
                continue
            source["sourceIds"] = [source["sourceId"]]
            by_identity[key] = source
            verified.append(source)
        except ReadError as exc:
            prior = next((r for r in old.values() if source["sourceId"] in r.get("sourceIds", [])), None)
            if prior:
                row = dict(prior, error=str(exc))
            else:
                row = error_row(source["sourceId"], source["label"], source["provider"], str(exc))
            row.update(errorCode=exc.code, retryAt=time.time() + exc.retry_after)
            errors.append(row)
    save_json(STATE / "identities.json", identity_cache)
    for source in verified:
        if source["provider"] == "claude":
            peers = [s for s in verified if s["provider"] == "claude" and
                     s.get("detail") == source.get("detail") and s.get("organizationName") == source.get("organizationName")]
            source["webFallbackAllowed"] = len(peers) == 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(6, len(verified)))) as pool:
        rows = list(pool.map(lambda s: read_usage(s, old), verified))
    ids = {row["id"] for row in rows}
    for row in errors:
        if row["id"] not in ids:
            rows.append(row)
            ids.add(row["id"])
    rows.sort(key=lambda r: (r["provider"] != "claude", r["label"].lower()))
    result = dict(accounts=rows, checkedAt=ISO())
    save_json(STATE / "snapshot.json", result)
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(snapshot()))
    except Exception:
        # Provider responses and credentials must never appear in error output.
        print(json.dumps({"accounts": [], "checkedAt": ISO(), "error": "Cannot read local subscription data"}))
        sys.exit(1)
