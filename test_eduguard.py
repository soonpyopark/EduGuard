"""자동 테스트:  python -m unittest -v test_eduguard

네트워크/레지스트리를 건드리지 않는다. (업스트림은 로컬 가짜 서버로 대체)
"""
import os
import socket
import tempfile
import threading
import unittest

from config_store import ConfigError, ConfigStore, REMOTE_SUPPORT_KEYWORDS, normalize_keyword, normalize_time
from proxy_server import FilterProxy, KeywordFilter

PRESET = ["ebs", "sevenedu", "megastudy", "etoos", "mimacstudy", "kollus", "cloudfront"]


class KeywordFilterTest(unittest.TestCase):
    def setUp(self):
        self.f = KeywordFilter(PRESET)

    def test_allowed_domains(self):
        for host in ["www.ebsi.co.kr", "mid.ebs.co.kr", "ebs.co.kr", "ebs-cdn.com",
                     "www.sevenedu.net", "cdn.sevenedu.net", "v.kollus.com",
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
        c.set_password("1234")
        c.set_settings(run_at_startup=True, start_in_tray=True, close_to_tray=False,
                       auto_start=False, port="9000", schedule_enabled=True,
                       schedule_start="23:00", schedule_end="06:30",
                       temp_unlock_minutes="45", watchdog_enabled=True,
                       log_to_file=True, log_retention_days="14",
                       block_message="공부 시간입니다.", guard_interval="1.5",
                       tray_notifications=False, password_max_fails="3",
                       password_lockout_seconds="60", audit_enabled=False,
                       remote_support_enabled=True)
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
            ("password_lockout_seconds", "4"), ("block_message", ""),
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


class UpdateCheckerTest(unittest.TestCase):
    def test_version_compare(self):
        import update_checker
        self.assertTrue(update_checker.is_newer("1.0.1", "1.0.0"))
        self.assertTrue(update_checker.is_newer("1.1.0", "1.0.9"))
        self.assertFalse(update_checker.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(update_checker.is_newer("0.9.9", "1.0.0"))

    def test_no_manifest_url_skips(self):
        import update_checker
        self.assertIsNone(update_checker.check_for_update(url=""))


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
                          b"GET http://www.ebsi.co.kr/a/b?x=1 HTTP/1.1\r\nHost: evil.com\r\n"
                          b"Proxy-Connection: keep-alive\r\nUser-Agent: t\r\n\r\n")
        self.assertIn(b"200 OK", resp)
        self.assertTrue(resp.endswith(b"hello"))
        sent = self.upstream.received[-1]
        self.assertTrue(sent.startswith(b"GET /a/b?x=1 HTTP/1.1\r\n"))
        self.assertIn(b"Host: www.ebsi.co.kr", sent)
        self.assertNotIn(b"evil.com", sent)
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


if __name__ == "__main__":
    unittest.main()
