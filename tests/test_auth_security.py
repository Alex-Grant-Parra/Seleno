"""Who a request is from, the emailed 2FA codes, reset links, and command sockets
replacing a stale connection."""
import time
import unittest
from unittest import mock

from harness import app, db, make_user

from security.client_ip import client_ip, is_local_request
from models.user import User


def ctx(remote, headers=None):
    return app.test_request_context("/", environ_base={"REMOTE_ADDR": remote}, headers=headers or {})


class ClientAddress(unittest.TestCase):
    def test_tunnel_reports_the_visitor(self):
        with ctx("127.0.0.1", {"CF-Connecting-IP": "93.184.216.34"}):
            self.assertEqual(client_ip(), "93.184.216.34")
            self.assertFalse(is_local_request())

    def test_forged_first_forwarded_entry_is_ignored(self):
        # The visitor wrote "127.0.0.1"; the tunnel appended their real address
        with ctx("127.0.0.1", {"X-Forwarded-For": "127.0.0.1, 93.184.216.34"}):
            self.assertEqual(client_ip(), "93.184.216.34")
            self.assertFalse(is_local_request())

    def test_headers_from_a_direct_connection_are_ignored(self):
        with ctx("151.101.1.69", {"CF-Connecting-IP": "127.0.0.1", "X-Forwarded-For": "10.0.0.1"}):
            self.assertEqual(client_ip(), "151.101.1.69")
            self.assertFalse(is_local_request())

    def test_this_machine_is_local(self):
        with ctx("127.0.0.1"):
            self.assertEqual(client_ip(), "127.0.0.1")
            self.assertTrue(is_local_request())

    def test_login_bypass_is_off_unless_configured(self):
        from controllers.auth import is_local_connection
        with ctx("127.0.0.1"):
            self.assertFalse(is_local_connection())
            with mock.patch.dict(app.config, {"LOCAL_AUTH_BYPASS": True}):
                self.assertTrue(is_local_connection())
        with ctx("127.0.0.1", {"X-Forwarded-For": "127.0.0.1, 93.184.216.34"}):
            with mock.patch.dict(app.config, {"LOCAL_AUTH_BYPASS": True}):
                self.assertFalse(is_local_connection())


class TwoFactorCodes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        cls.user = make_user("twofa_user")

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def test_right_code_works_once(self):
        code = self.user.generate_totp_code()
        self.assertEqual(len(code), 6)
        self.assertTrue(self.user.verify_2fa_code(code.upper()))
        self.assertFalse(self.user.verify_2fa_code(code))

    def test_wrong_guesses_are_capped(self):
        code = self.user.generate_totp_code()
        for _ in range(User.TWO_FA_MAX_ATTEMPTS):
            self.assertFalse(self.user.verify_2fa_code("zzzzzz"))
        self.assertIsNone(self.user.current_2fa_code)
        self.assertFalse(self.user.verify_2fa_code(code))

    def test_code_expires(self):
        code = self.user.generate_totp_code()
        later = time.time() + User.TWO_FA_CODE_TTL_S + 1
        with mock.patch("models.user.time.time", return_value=later):
            self.assertFalse(self.user.verify_2fa_code(code))


class ResetLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        cls.user = make_user("reset_user")

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def test_link_stops_working_after_the_password_changes(self):
        token = self.user.get_reset_token()
        self.assertEqual(User.verify_reset_token(token).id, self.user.id)
        self.user.password = "a different hash"
        db.session.commit()
        self.assertIsNone(User.verify_reset_token(token))

    def test_garbage_is_refused(self):
        self.assertIsNone(User.verify_reset_token("not-a-token"))


class CommandSocketReplacement(unittest.TestCase):
    def test_reconnect_replaces_and_old_cleanup_keeps_new(self):
        from app.WebsocketServer import ClientManager
        manager = ClientManager()
        old, new = object(), object()
        self.assertIsNone(manager.replace_client("scope", old))
        self.assertIs(manager.replace_client("scope", new), old)
        manager.remove_client("scope", old)  # the old handler's cleanup
        self.assertIs(manager.get_client("scope").ws, new)
        manager.remove_client("scope", new)
        self.assertIsNone(manager.get_client("scope"))


if __name__ == "__main__":
    unittest.main()
