"""자동 테스트:  python -m unittest -v test_eduguard

네트워크/레지스트리를 건드리지 않는다. (업스트림은 로컬 가짜 서버로 대체)
"""
import os
import json
import socket
import tempfile
import threading
import unittest

from config_store import ConfigError, ConfigStore, REMOTE_SUPPORT_KEYWORDS, normalize_keyword, normalize_time
from proxy_server import FilterProxy, KeywordFilter, TRUSTED_LOCAL_HOSTS, _is_trusted_local_host

PRESET = [
    "ebs", "sevenedu", "starplayer", "axissoft", "jwplatform", "akamaized", "cloudfront",
    "cdnetworks", "kollus", "starplayerplus", "megastudy", "etoos", "mimacstudy",
]


class KeywordFilterTest(unittest.TestCase):
    def setUp(self):
        self.f = KeywordFilter(PRESET)

    def test_allowed_domains(self):
        for host in ["www.ebsi.co.kr", "mid.ebs.co.kr", "ebs.co.kr", "ebs-cdn.com",
                     "www.sevenedu.net", "cdn.sevenedu.net", "content.jwplatform.com", "v.kollus.com",
                     "d1234.cloudfront.net", "WWW.EBS.CO.KR", "ebs.co.kr."]:
            self.assertTrue(self.f.is_allowed(host), host)

    def test_blocked_domains(self):
        for host in ["google.com", "www.youtube.com", "naver.com", "discord.com", "", "localhost"]:
            self.assertFalse(self.f.is_allowed(host), host)

    def test_ip_literals_blocked(self):
        for host in ["127.0.0.1", "8.8.8.8", "[::1]", "2001:db8::1"]:
            self.assertFalse(self.f.is_allowed(host), host)

    def test_live_update(self):
        self.assertFalse(self.f.is_allowed("www.khanacademy.org"))
        self.f.set_keywords(PRESET + ["khanacademy"])
        self.assertTrue(self.f.is_allowed("www.khanacademy.org"))


class ConfigStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "config.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_defaults_and_password(self):
        c = ConfigStore(self.path)
        self.assertEqual(c.keywords, PRESET)
        self.assertFalse(c.has_password())
        c.set_password("1234")
        self.assertTrue(c.verify_password("1234"))
        self.assertFalse(c.verify_password("0000"))
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn("1234", f.read())  # 평문 저장 금지

    def test_persistence_and_keywords(self):
        c = ConfigStore(self.path)
        c.set_password("1234")
        c.add_keyword(" KhanAcademy ")
        c.remove_keyword("etoos")
        c2 = ConfigStore(self.path)
        self.assertIn("khanacademy", c2.keywords)
        self.assertNotIn("etoos", c2.keywords)
        self.assertTrue(c2.verify_password("1234"))
        self.assertFalse(c2.tampered)

    def test_invalid_keywords(self):
        for bad in ["", "  ", "co", "com", "a b", "한글키워드", "ebs/../", "https"]:
            with self.assertRaises(ConfigError, msg=bad):
                normalize_keyword(bad)
        c = ConfigStore(self.path)
        with self.assertRaises(ConfigError):
            c.add_keyword("ebs")  # 중복

    def test_tamper_restores_backup(self):
        c = ConfigStore(self.path)
        c.set_password("1234")
        c.add_keyword("khanacademy")  # 이 시점에 직전 정상본이 .bak 으로 저장됨
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text.replace("khanacademy", "youtube"))  # 몰래 키워드 편집
        c2 = ConfigStore(self.path)
        self.assertTrue(c2.tampered)
        self.assertNotIn("youtube", c2.keywords)
        self.assertTrue(c2.verify_password("1234"))

    def test_tamper_without_backup_resets(self):
        c = ConfigStore(self.path)
        c.set_password("1234")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{garbage")
        if os.path.exists(self.path + ".bak"):
            os.remove(self.path + ".bak")
        c2 = ConfigStore(self.path)
        self.assertTrue(c2.tampered)
        self.assertFalse(c2.has_password())

    def test_settings_defaults_and_persistence(self):
        c = ConfigStore(self.path)
        self.assertFalse(c.run_at_startup)
        self.assertFalse(c.start_in_tray)
        self.assertTrue(c.close_to_tray)
        self.assertTrue(c.auto_start)
        self.assertFalse(c.schedule_enabled)
        self.assertEqual(c.schedule_start, "16:00")
        self.assertEqual(c.schedule_end, "22:00")
        self.assertEqual(c.temp_unlock_minutes, 30)
        self.assertFalse(c.watchdog_enabled)
        self.assertFalse(c.log_to_file)
        self.assertEqual(c.log_retention_days, 30)
        self.assertEqual(c.guard_interval, 2.0)
        self.assertTrue(c.tray_notifications)
        self.assertEqual(c.password_max_fails, 5)
        self.assertEqual(c.password_lockout_seconds, 30)
        self.assertTrue(c.audit_enabled)
        self.assertFalse(c.remote_support_enabled)
        self.assertTrue(c.suppress_repeated_logs)
        self.assertEqual(c.repeat_log_window_seconds, 30)
        self.assertEqual(c.block_start_delay_seconds, 90)
        self.assertTrue(c.wait_for_starplayer)
        self.assertEqual(c.starplayer_wait_timeout_seconds, 120)
        c.set_password("1234")
        c.set_settings(run_at_startup=True, start_in_tray=True, close_to_tray=False,
                       auto_start=False, port="9000", schedule_enabled=True,
                       schedule_start="23:00", schedule_end="06:30",
                       temp_unlock_minutes="45", watchdog_enabled=True,
                       log_to_file=True, log_retention_days="14",
                       block_message="공부 시간입니다.", guard_interval="1.5",
                       tray_notifications=False, password_max_fails="3",
                       password_lockout_seconds="60", audit_enabled=False,
                       remote_support_enabled=True, suppress_repeated_logs=False,
                       repeat_log_window_seconds="45",
                       block_start_delay_seconds="60", wait_for_starplayer=False,
                       starplayer_wait_timeout_seconds="180")
        c2 = ConfigStore(self.path)
        self.assertFalse(c2.tampered)
        self.assertTrue(c2.run_at_startup)
        self.assertTrue(c2.start_in_tray)
        self.assertFalse(c2.close_to_tray)
        self.assertFalse(c2.auto_start)
        self.assertEqual(c2.port, 9000)
        self.assertTrue(c2.schedule_enabled)
        self.assertEqual(c2.schedule_start, "23:00")
        self.assertEqual(c2.schedule_end, "06:30")
        self.assertEqual(c2.temp_unlock_minutes, 45)
        self.assertTrue(c2.watchdog_enabled)
        self.assertTrue(c2.log_to_file)
        self.assertEqual(c2.log_retention_days, 14)
        self.assertEqual(c2.block_message, "공부 시간입니다.")
        self.assertEqual(c2.guard_interval, 1.5)
        self.assertFalse(c2.tray_notifications)
        self.assertEqual(c2.password_max_fails, 3)
        self.assertEqual(c2.password_lockout_seconds, 60)
        self.assertFalse(c2.audit_enabled)
        self.assertTrue(c2.remote_support_enabled)
        self.assertFalse(c2.suppress_repeated_logs)
        self.assertEqual(c2.repeat_log_window_seconds, 45)
        self.assertEqual(c2.block_start_delay_seconds, 60)
        self.assertFalse(c2.wait_for_starplayer)
        self.assertEqual(c2.starplayer_wait_timeout_seconds, 180)

    def test_settings_validation_is_atomic(self):
        c = ConfigStore(self.path)
        for bad_port in ["abc", "80", "70000", ""]:
            with self.assertRaises(ConfigError, msg=bad_port):
                c.set_settings(start_in_tray=True, port=bad_port)
        self.assertFalse(c.start_in_tray)  # 한 항목이 잘못되면 전부 저장하지 않음
        for bad_time in ["24:00", "9:00", "aa:bb", ""]:
            with self.assertRaises(ConfigError, msg=bad_time):
                normalize_time(bad_time)
        for key, bad in [
            ("temp_unlock_minutes", "0"), ("log_retention_days", "366"),
            ("guard_interval", "0.1"), ("password_max_fails", "21"),
            ("password_lockout_seconds", "4"), ("repeat_log_window_seconds", "4"),
            ("block_start_delay_seconds", "601"), ("starplayer_wait_timeout_seconds", "-1"),
            ("block_message", ""),
        ]:
            with self.assertRaises(ConfigError, msg=key):
                c.set_settings(**{key: bad})
        with self.assertRaises(ConfigError):
            c.set_settings(unknown_option=True)

    def test_set_keywords_replaces_and_validates(self):
        c = ConfigStore(self.path)
        c.set_keywords([" EBS ", "khanacademy", "ebs"])
        self.assertEqual(c.keywords, ["ebs", "khanacademy"])
        with self.assertRaises(ConfigError):
            c.set_keywords(["com"])

    def test_old_config_gets_new_default_keywords_once(self):
        c = ConfigStore(self.path)
        c.set_password("1234")
        with open(self.path, encoding="utf-8") as f:
            doc = json.load(f)
        payload = doc["payload"]
        payload["keywords"] = ["ebs", "sevenedu"]
        payload.pop("default_keywords_revision", None)
        doc = {"payload": payload, "mac": ConfigStore._mac(payload)}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(doc, f)

        migrated = ConfigStore(self.path)
        self.assertIn("jwplatform", migrated.keywords)
        self.assertIn("axissoft", migrated.keywords)


class StarPlayerReadyTest(unittest.TestCase):
    def test_detects_marker_in_process_names(self):
        import starplayer_ready
        self.assertTrue(starplayer_ready.is_starplayer_ready(
            process_names=["chrome.exe", "StarPlayer.exe", "explorer.exe"]))
        self.assertTrue(starplayer_ready.is_starplayer_ready(
            process_names=["AxisSoftHelper.exe"]))
        self.assertFalse(starplayer_ready.is_starplayer_ready(
            process_names=["chrome.exe", "notepad.exe"]))

    def test_parse_tasklist_csv(self):
        import starplayer_ready
        sample = '"StarPlayer.exe","1234","Console","1","12,345 K"\r\n"chrome.exe","1","Console","1","1 K"\r\n'
        names = starplayer_ready.list_running_process_names(sample)
        self.assertIn("StarPlayer.exe", names)
        self.assertIn("chrome.exe", names)


class StaleProxyDetectTest(unittest.TestCase):
    def test_looks_like_eduguard_proxy(self):
        from system_proxy import SystemProxy
        import winreg
        self.assertTrue(SystemProxy.looks_like_eduguard_proxy({
            "ProxyEnable": [winreg.REG_DWORD, 1],
            "ProxyServer": [winreg.REG_SZ, "127.0.0.1:8899"],
        }))
        self.assertFalse(SystemProxy.looks_like_eduguard_proxy({
            "ProxyEnable": [winreg.REG_DWORD, 0],
            "ProxyServer": [winreg.REG_SZ, "127.0.0.1:8899"],
        }))
        self.assertFalse(SystemProxy.looks_like_eduguard_proxy({
            "ProxyEnable": [winreg.REG_DWORD, 1],
            "ProxyServer": [winreg.REG_SZ, "proxy.company.com:8080"],
        }))
        self.assertFalse(SystemProxy.looks_like_eduguard_proxy({}))

    def test_summarize_and_status_report(self):
        from system_proxy import SystemProxy
        import winreg
        import tempfile
        import os
        values = {
            "ProxyEnable": [winreg.REG_DWORD, 1],
            "ProxyServer": [winreg.REG_SZ, "127.0.0.1:8899"],
            "ProxyOverride": [winreg.REG_SZ, ""],
            "AutoConfigURL": None,
        }
        summary = SystemProxy.summarize_values(values)
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["server"], "127.0.0.1:8899")
        self.assertTrue(summary["eduguard_like"])

        with tempfile.TemporaryDirectory() as tmp:
            sp = SystemProxy(os.path.join(tmp, "proxy_backup.json"))
            sp._target = "127.0.0.1:8899"
            sp.read_user_snapshots = lambda: [{
                "sid": "S-1-5-21-111-222-333-1001",
                **summary,
            }]
            report = sp.status_report(app_blocking=True, expected_port=8899)
            self.assertIn("앱 차단 상태: 켜짐", report)
            self.assertIn("127.0.0.1:8899", report)
            self.assertIn("EduGuard 강제 설정과 일치", report)

            report2 = sp.status_report(app_blocking=False)
            self.assertIn("잔여 EduGuard 프록시 가능", report2)


class StartupTaskTest(unittest.TestCase):
    def test_task_xml(self):
        import xml.etree.ElementTree as ET
        import startup
        exe = r"C:\Program Files\Edu & Guard\EduGuard.exe"
        xml = startup.build_task_xml(exe, [r"C:\EduGuard\main.py"])
        root = ET.fromstring(xml.split("?>", 1)[1])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find(".//t:Exec/t:Command", ns).text, exe)  # '&' 이스케이프 후 복원
        self.assertEqual(root.find(".//t:RunLevel", ns).text, "HighestAvailable")
        self.assertIsNotNone(root.find(".//t:LogonTrigger", ns))
        self.assertEqual(root.find(".//t:DisallowStartIfOnBatteries", ns).text, "false")
        self.assertEqual(root.find(".//t:ExecutionTimeLimit", ns).text, "PT0S")

    def test_cleanup_command_and_xml(self):
        import xml.etree.ElementTree as ET
        import startup
        exe, args = startup.launch_cleanup_command()
        self.assertIn("--clear-stale-proxy", args)
        xml = startup.build_task_xml(
            exe, args, delay="PT0S",
            description="EduGuard - 로그온 직후 잔여 시스템 프록시 복원",
        )
        root = ET.fromstring(xml.split("?>", 1)[1])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find(".//t:Delay", ns).text, "PT0S")
        self.assertIn("--clear-stale-proxy", root.find(".//t:Arguments", ns).text)

    def test_frozen_cleanup_workdir_uses_exe_dir(self):
        """MSI 배포처럼 인자가 플래그만일 때 WorkingDirectory 가 비면 안 된다."""
        import xml.etree.ElementTree as ET
        import startup
        exe = r"C:\Program Files\EduGuard\EduGuard.exe"
        xml = startup.build_task_xml(exe, ["--clear-stale-proxy"], delay="PT0S")
        root = ET.fromstring(xml.split("?>", 1)[1])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(
            root.find(".//t:WorkingDirectory", ns).text,
            r"C:\Program Files\EduGuard",
        )

    def test_main_register_xml_has_delay(self):
        import xml.etree.ElementTree as ET
        import startup
        xml = startup.build_task_xml(
            r"C:\EduGuard\EduGuard.exe", [], delay="PT15S",
            description="EduGuard - Windows 로그온 시 자동 실행",
        )
        root = ET.fromstring(xml.split("?>", 1)[1])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find(".//t:Delay", ns).text, "PT15S")


class UpdateCheckerTest(unittest.TestCase):
    def test_version_compare(self):
        import update_checker
        self.assertTrue(update_checker.is_newer("1.0.1", "1.0.0"))
        self.assertTrue(update_checker.is_newer("1.1.0", "1.0.9"))
        self.assertFalse(update_checker.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(update_checker.is_newer("0.9.9", "1.0.0"))
        self.assertTrue(update_checker.is_newer("1.0.0.3", "1.0.0.2"))
        self.assertFalse(update_checker.is_newer("1.0.0.0", "1.0.0"))

    def test_no_manifest_url_skips(self):
        import update_checker
        self.assertIsNone(update_checker.check_for_update(url=""))


class VersionDisplayTest(unittest.TestCase):
    def test_semver_and_git_tag(self):
        import version
        self.assertEqual(version.app_semver("1.0.0.2"), "1.0.0-2")
        self.assertEqual(version.app_git_tag("1.0.0.2"), "v1.0.0-2")
        self.assertEqual(version.app_semver("1.0.0"), "1.0.0")

    def test_version_display_source(self):
        import version
        # 빌드 산출물이 없는 소스 실행이면 빌드 시각 문구가 없다.
        self.assertTrue(version.APP_VERSION)
        if not version.APP_BUILD_TIME:
            self.assertIn("소스 실행", version.version_display())
        else:
            self.assertIn(version.APP_BUILD_TIME, version.version_display())

    def test_write_build_info_content(self):
        import importlib.util
        from pathlib import Path
        from scripts.build_msi import write_build_info
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build_info.py"
            write_build_info(path, "2026-10-14 21:20:36")
            spec = importlib.util.spec_from_file_location("build_info_test", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertEqual(mod.BUILD_TIME, "2026-10-14 21:20:36")


class RemoteSupportModeTest(unittest.TestCase):
    def test_remote_support_keywords_cover_teamviewer_only(self):
        f = KeywordFilter(PRESET + REMOTE_SUPPORT_KEYWORDS)
        self.assertTrue(f.is_allowed("router.teamviewer.com"))
        self.assertTrue(f.is_allowed("server123.dyngate.com"))
        self.assertFalse(f.is_allowed("remotedesktop.google.com"))
        self.assertFalse(f.is_allowed("www.gstatic.com"))


class FakeUpstream:
    """받은 데이터를 기록하고 고정 응답을 돌려주는 가짜 서버."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(5)
        self.port = self.srv.getsockname()[1]
        self.received = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            data = conn.recv(65536)
            self.received.append(data)
            if data.startswith(b"GET"):
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
            else:
                conn.sendall(b"pong:" + data)  # CONNECT 터널 에코

    def close(self):
        self.srv.close()


def _roundtrip(port, payload: bytes, read_until_close=True, timeout=5) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(payload)
    out = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
            if not read_until_close:
                break
    except socket.timeout:
        pass
    s.close()
    return out


class ProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = FakeUpstream()
        cls.events = []
        cls.proxy = FilterProxy(
            PRESET, port=0, on_event=cls.events.append,
            connect_func=lambda host, port: socket.create_connection(("127.0.0.1", cls.upstream.port), 5),
        )
        cls.proxy.start()

    @classmethod
    def tearDownClass(cls):
        cls.proxy.stop()
        cls.upstream.close()

    def test_https_connect_blocked(self):
        resp = _roundtrip(self.proxy.port, b"CONNECT www.google.com:443 HTTP/1.1\r\nHost: www.google.com:443\r\n\r\n")
        self.assertIn(b"403 Forbidden", resp)

    def test_http_blocked(self):
        resp = _roundtrip(self.proxy.port, b"GET http://www.youtube.com/ HTTP/1.1\r\nHost: www.youtube.com\r\n\r\n")
        self.assertIn(b"403 Forbidden", resp)
        self.assertNotIn(b"hello", resp)

    def test_custom_block_message(self):
        self.proxy.set_block_message("공부 시간입니다.")
        try:
            resp = _roundtrip(self.proxy.port, b"GET http://www.youtube.com/ HTTP/1.1\r\nHost: www.youtube.com\r\n\r\n")
            self.assertIn("공부 시간입니다.".encode("utf-8"), resp)
        finally:
            self.proxy.set_block_message("허용된 학습 사이트가 아닙니다.")

    def test_http_allowed_and_rewritten(self):
        resp = _roundtrip(self.proxy.port,
                          b"GET http://www.ebsi.co.kr/a/b?x=1 HTTP/1.1\r\nHost: spoofed.invalid\r\n"
                          b"Proxy-Connection: keep-alive\r\nUser-Agent: t\r\n\r\n")
        self.assertIn(b"200 OK", resp)
        self.assertTrue(resp.endswith(b"hello"))
        sent = self.upstream.received[-1]
        self.assertTrue(sent.startswith(b"GET /a/b?x=1 HTTP/1.1\r\n"))
        self.assertIn(b"Host: www.ebsi.co.kr", sent)
        self.assertNotIn(b"spoofed.invalid", sent)
        self.assertNotIn(b"Proxy-Connection", sent)
        self.assertIn(b"Connection: close", sent)

    def test_https_connect_allowed_tunnel(self):
        s = socket.create_connection(("127.0.0.1", self.proxy.port), timeout=5)
        s.sendall(b"CONNECT cdn.sevenedu.net:443 HTTP/1.1\r\nHost: cdn.sevenedu.net:443\r\n\r\n")
        head = s.recv(4096)
        self.assertIn(b"200 Connection Established", head)
        s.sendall(b"ping")
        self.assertEqual(s.recv(4096), b"pong:ping")
        s.close()

    def test_ip_connect_blocked(self):
        resp = _roundtrip(self.proxy.port, b"CONNECT 142.250.0.1:443 HTTP/1.1\r\n\r\n")
        self.assertIn(b"403 Forbidden", resp)

    def test_direct_request_rejected(self):
        resp = _roundtrip(self.proxy.port, b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        self.assertIn(b"400", resp)

    def test_counters_and_events(self):
        allowed0, blocked0 = self.proxy.allowed_count, self.proxy.blocked_count
        _roundtrip(self.proxy.port, b"CONNECT www.google.com:443 HTTP/1.1\r\n\r\n")
        _roundtrip(self.proxy.port, b"GET http://www.ebs.co.kr/ HTTP/1.1\r\n\r\n")
        self.assertEqual(self.proxy.blocked_count, blocked0 + 1)
        self.assertEqual(self.proxy.allowed_count, allowed0 + 1)
        self.assertTrue(any(e["action"] == "BLOCK" and e["host"] == "www.google.com" for e in self.events))
        self.assertTrue(any(e["action"] == "ALLOW" and e["host"] == "www.ebs.co.kr" for e in self.events))


class TrustedLocalHostTest(unittest.TestCase):
    def test_axissoft_localhost_is_trusted(self):
        self.assertTrue(_is_trusted_local_host("localhost.axissoft.co.kr", TRUSTED_LOCAL_HOSTS))
        self.assertTrue(_is_trusted_local_host("LOCALHOST.AXISSOFT.CO.KR.", TRUSTED_LOCAL_HOSTS))
        self.assertFalse(_is_trusted_local_host("localhost.example.com", TRUSTED_LOCAL_HOSTS))


class FakeProxy:
    def __init__(self):
        self.filter = KeywordFilter(PRESET)
        self.port = 8899
        self.allowed_count = 0
        self.blocked_count = 0
        self.running = False
        self.block_message = ""

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def set_block_message(self, message):
        self.block_message = message


class FakeSysProxy:
    def __init__(self, stale=False):
        self.enabled_port = None
        self.stale = stale
        self.disable_calls = 0

    def enable(self, port):
        self.enabled_port = port

    def disable(self):
        self.enabled_port = None
        self.stale = False
        self.disable_calls += 1

    def enforce(self):
        return False

    def has_stale_proxy(self):
        return self.stale

    def status_report(self, **kwargs):
        return f"report blocking={kwargs.get('app_blocking')}"


class FakeStartup:
    def __init__(self):
        self.registered = False

    def is_registered(self):
        return self.registered

    def register(self):
        self.registered = True
        return True, ""

    def unregister(self):
        self.registered = False
        return True, ""

    def register_cleanup(self):
        return True, ""

    def launch_command(self):
        return "EduGuardCore.exe", []


class FakeClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_engine(tmpdir, *, password="1234", starplayer=True, stale=False, **settings):
    from engine import Engine
    cfg = ConfigStore(os.path.join(tmpdir, "config.json"))
    if password:
        cfg.set_password(password)
    defaults = {"audit_enabled": False, "block_start_delay_seconds": 0}
    defaults.update(settings)
    cfg.set_settings(**defaults)
    clock = FakeClock()
    ready = {"value": starplayer}
    engine = Engine(
        cfg, tmpdir, proxy=FakeProxy(), sysproxy=FakeSysProxy(stale=stale), admin=True,
        starplayer_check=lambda: ready["value"], startup_module=FakeStartup(), clock=clock,
    )
    return engine, clock, ready


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def test_block_start_stop_requires_password(self):
        from engine import AuthError
        engine, _, _ = make_engine(self.dir.name)
        engine.start_blocking()
        self.assertEqual(engine.status(), "blocking")
        self.assertEqual(engine.sysproxy.enabled_port, 8899)
        with self.assertRaises(AuthError):
            engine.stop_blocking("wrong")
        self.assertEqual(engine.status(), "blocking")
        engine.stop_blocking("1234")
        self.assertEqual(engine.status(), "off")
        self.assertIsNone(engine.sysproxy.enabled_port)
        engine.guard.stop()

    def test_password_lockout(self):
        from engine import LockedError
        engine, clock, _ = make_engine(self.dir.name, password_max_fails=2, password_lockout_seconds=30)
        with self.assertRaises(Exception):
            engine.verify("x", "test")
        with self.assertRaises(LockedError):
            engine.verify("x", "test")
        with self.assertRaises(LockedError):
            engine.verify("1234", "test")  # 잠금 중에는 맞는 비밀번호도 거부
        clock.now += 31
        engine.verify("1234", "test")

    def test_temp_unlock_expires_and_reblocks(self):
        engine, clock, _ = make_engine(self.dir.name)
        engine.start_blocking()
        engine.temp_unlock("1234", 5)
        self.assertEqual(engine.status(), "temp_unlock")
        self.assertEqual(engine.state()["temp_unlock_remaining"], 5)
        clock.now += 5 * 60 + 1
        engine.tick()
        self.assertEqual(engine.status(), "blocking")
        engine.guard.stop()

    def test_auto_block_waits_for_starplayer(self):
        engine, clock, ready = make_engine(self.dir.name, starplayer=False, starplayer_wait_timeout_seconds=60)
        engine._schedule_auto_block()
        engine.tick()
        self.assertEqual(engine.status(), "waiting")
        ready["value"] = True
        clock.now += 6
        engine.tick()
        self.assertEqual(engine.status(), "blocking")
        engine.guard.stop()

    def test_auto_block_forced_after_timeout(self):
        engine, clock, _ = make_engine(self.dir.name, starplayer=False, starplayer_wait_timeout_seconds=10)
        engine._schedule_auto_block()
        engine.tick()
        self.assertEqual(engine.status(), "waiting")
        clock.now += 11
        engine.tick()
        self.assertEqual(engine.status(), "blocking")
        engine.guard.stop()

    def test_stale_proxy_cleared_on_start(self):
        engine, _, _ = make_engine(self.dir.name, stale=True)
        engine.clear_stale_proxy_if_needed()
        self.assertEqual(engine.sysproxy.disable_calls, 1)
        self.assertTrue(any(e.get("host") == "시스템 프록시" for e in engine.poll()["events"]))

    def test_poll_returns_only_new_events(self):
        engine, _, _ = make_engine(self.dir.name, suppress_repeated_logs=False)
        engine._log("GUARD", "a", "1")
        first = engine.poll(0)
        engine._log("GUARD", "b", "2")
        second = engine.poll(first["last_id"])
        self.assertEqual([e["host"] for e in second["events"]], ["b"])
        self.assertEqual(engine.poll(10_000)["last_id"], second["last_id"])  # 엔진 재시작 후 큰 번호면 처음부터

    def test_keywords_and_settings(self):
        engine, _, _ = make_engine(self.dir.name)
        engine.add_keyword("1234", "khanacademy")
        self.assertTrue(engine.test_domain("https://www.khanacademy.org/math")["allowed"])
        engine.remove_keywords("1234", ["khanacademy"])
        self.assertFalse(engine.test_domain("www.khanacademy.org")["allowed"])
        loaded = engine.get_settings("1234")["settings"]
        self.assertEqual(loaded["port"], 8899)
        engine.save_settings("1234", {"run_at_startup": True, "temp_unlock_minutes": "45", "unknown": 1})
        self.assertEqual(engine.cfg.temp_unlock_minutes, 45)
        self.assertTrue(engine._startup.registered)

    def test_setup_password_only_once(self):
        from engine import EngineError
        engine, _, _ = make_engine(self.dir.name, password=None)
        self.assertFalse(engine.state()["has_password"])
        engine.setup_password("abcd")
        self.assertTrue(engine.state()["has_password"])
        with self.assertRaises(EngineError):
            engine.setup_password("efgh")

    def test_exit_restores_proxy(self):
        engine, _, _ = make_engine(self.dir.name)
        engine.start_blocking()
        engine.request_exit("1234")
        self.assertTrue(engine.exit_requested.is_set())
        self.assertIsNone(engine.sysproxy.enabled_port)


class ApiServerTest(unittest.TestCase):
    def setUp(self):
        from api_server import ApiServer
        self.dir = tempfile.TemporaryDirectory()
        self.engine, _, _ = make_engine(self.dir.name)
        self.server = ApiServer(self.engine, "secret-token")
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.engine.guard.stop()
        self.dir.cleanup()

    def _request(self, method, path, body=None, token="secret-token", host=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-EduGuard-Token"] = token
        if host:
            headers["Host"] = host
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_rejects_missing_token_and_foreign_host(self):
        self.assertEqual(self._request("GET", "/api/ping", token=None)[0], 403)
        self.assertEqual(self._request("GET", "/api/ping", token="nope")[0], 403)
        self.assertEqual(self._request("GET", "/api/ping", host="evil.example:80")[0], 403)

    def test_block_flow_over_http(self):
        status, data = self._request("POST", "/api/block/start", {})
        self.assertEqual(status, 200)
        self.assertEqual(self._request("GET", "/api/poll?since=0")[1]["state"]["status"], "blocking")
        status, data = self._request("POST", "/api/block/stop", {"password": "bad"})
        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])
        status, _ = self._request("POST", "/api/block/stop", {"password": "1234"})
        self.assertEqual(status, 200)
        self.assertEqual(self._request("GET", "/api/poll")[1]["state"]["status"], "off")

    def test_domain_test_and_unknown_route(self):
        status, data = self._request("POST", "/api/test-domain", {"domain": "www.ebs.co.kr"})
        self.assertEqual(status, 200)
        self.assertTrue(data["allowed"])
        self.assertEqual(self._request("GET", "/api/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
