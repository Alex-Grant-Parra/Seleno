"""Sharing a telescope: the owner gives another user standing access by username.
They use it at any time without a booking, as the owner would, but can't manage
its bookings, remove it, or share it on."""
import unittest
from datetime import datetime, timedelta

from harness import app, db, make_user, make_telescope, make_booking, login, select

from app.telescope_access import CONTROL, OWNER, has_shared_access, user_can_control
from models.telescope_share import TelescopeShare


def now():
    return datetime.utcnow()


class Sharing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        cls.owner = make_user("share_owner")
        cls.friend = make_user("Share_Friend")
        cls.stranger = make_user("share_stranger")
        cls.limited = make_user("share_limited", "Limited")
        cls.booker = make_user("share_booker")
        cls.scope = make_telescope("share-scope", owner=cls.owner)

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()

    def tearDown(self):
        TelescopeShare.query.delete()
        db.session.commit()

    def as_user(self, user, telescope="share-scope"):
        c = app.test_client()
        login(c, user)
        select(c, telescope)
        return c

    def share(self, actor, username, telescope=None):
        telescope = telescope or self.scope
        return self.as_user(actor).post(f"/telescopes/{telescope.id}/share", data={"username": username})

    def shares(self, telescope=None):
        return TelescopeShare.query.filter_by(telescope_id=(telescope or self.scope).id).all()

    def test_owner_shares_by_username_and_friend_controls_without_booking(self):
        self.assertFalse(user_can_control(self.friend, self.scope, CONTROL))
        r = self.share(self.owner, "share_friend")  # case forgiven when only one account matches
        self.assertEqual(r.status_code, 302)
        rows = self.shares()
        self.assertEqual([(s.user_id, s.granted_by_user_id) for s in rows], [(self.friend.id, self.owner.id)])
        self.assertTrue(user_can_control(self.friend, self.scope, CONTROL))
        self.assertTrue(user_can_control(self.friend, self.scope, OWNER))
        # Even while someone else's booking covers now
        booking = make_booking(self.scope, self.booker, now() - timedelta(hours=1), now() + timedelta(hours=1))
        try:
            self.assertTrue(user_can_control(self.friend, self.scope, CONTROL))
        finally:
            db.session.delete(booking)
            db.session.commit()

    def test_status_reports_shared_access(self):
        # The interface's status poll: refused before sharing, then shared access with owner-level controls
        self.assertEqual(self.as_user(self.friend).get("/interface/telescope_status").status_code, 403)
        self.share(self.owner, "Share_Friend")
        r = self.as_user(self.friend).get("/interface/telescope_status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["access"], {"control": True, "owner": True, "shared": True})
        self.assertEqual(self.as_user(self.owner).get("/interface/telescope_status").get_json()["access"],
                         {"control": True, "owner": True, "shared": False})

    def test_share_by_email(self):
        # make_user gives <username>@example.test; any case, surrounding spaces ignored
        self.share(self.owner, "  share_friend@EXAMPLE.test ")
        self.assertEqual([s.user_id for s in self.shares()], [self.friend.id])
        self.share(self.owner, "nobody@example.test")
        self.share(self.owner, "share_limited@example.test")
        self.assertEqual([s.user_id for s in self.shares()], [self.friend.id])

    def test_only_the_owner_can_share(self):
        self.share(self.owner, "Share_Friend")
        # Neither someone it's shared with nor a stranger can add people
        self.share(self.friend, "share_stranger")
        self.share(self.stranger, "share_booker")
        self.assertEqual([s.user_id for s in self.shares()], [self.friend.id])

    def test_refuses_unknown_self_limited_and_duplicates(self):
        for name in ("nobody_by_that_name", "share_owner", "share_limited", ""):
            self.share(self.owner, name)
        self.assertEqual(self.shares(), [])
        self.share(self.owner, "Share_Friend")
        self.share(self.owner, "Share_Friend")
        self.assertEqual(len(self.shares()), 1)

    def test_owner_revokes_and_friend_can_leave(self):
        self.share(self.owner, "Share_Friend")
        share = self.shares()[0]
        # A stranger can't remove it
        self.as_user(self.stranger).post(f"/telescopes/{self.scope.id}/share/{share.id}/remove")
        self.assertEqual(len(self.shares()), 1)
        self.as_user(self.owner).post(f"/telescopes/{self.scope.id}/share/{share.id}/remove")
        self.assertEqual(self.shares(), [])
        self.assertFalse(user_can_control(self.friend, self.scope, CONTROL))

        self.share(self.owner, "Share_Friend")
        share = self.shares()[0]
        self.as_user(self.friend).post(f"/telescopes/{self.scope.id}/share/{share.id}/remove")
        self.assertEqual(self.shares(), [])

    def test_share_lapses_when_ownership_changes(self):
        scope = make_telescope("share-handover", owner=self.owner)
        self.share(self.owner, "Share_Friend", telescope=scope)
        self.assertTrue(has_shared_access(self.friend, scope))
        scope.user_id = self.stranger.id
        db.session.commit()
        self.assertFalse(has_shared_access(self.friend, scope))
        self.assertFalse(user_can_control(self.friend, scope, CONTROL))

    def test_shares_go_with_a_deleted_telescope(self):
        scope = make_telescope("share-doomed", owner=self.owner)
        self.share(self.owner, "Share_Friend", telescope=scope)
        scope_id = scope.id
        db.session.delete(scope)
        db.session.commit()
        self.assertEqual(TelescopeShare.query.filter_by(telescope_id=scope_id).count(), 0)

    def test_friend_cannot_remove_the_telescope(self):
        scope = make_telescope("share-keep", owner=self.owner)
        self.share(self.owner, "Share_Friend", telescope=scope)
        r = self.as_user(self.friend, "share-keep").post("/interface/remove_telescope", json={"telescope_id": "share-keep"})
        self.assertEqual(r.status_code, 403)
        self.assertIsNotNone(db.session.get(type(scope), scope.id))

    def test_friend_starts_a_plan_without_booking_and_receives_photos(self):
        from app.capture_plans import prepare_plan
        from controllers.telescope_uploads import _owner_may_receive
        plan_input = {"name": "M31", "steps": [{"type": "bias", "count": 2}]}
        _, _, errors = prepare_plan(self.stranger, self.scope, plan_input)
        self.assertTrue(errors)
        self.share(self.owner, "Share_Friend")
        plan, context, errors = prepare_plan(self.friend, self.scope, plan_input)
        self.assertEqual(errors, [])
        self.assertEqual(context["owner_id"], self.friend.id)  # the photos are theirs
        self.assertIsNone(context["booking_id"])
        self.assertTrue(_owner_may_receive(self.friend.id, self.scope, now()))
        self.assertFalse(_owner_may_receive(self.stranger.id, self.scope, now()))

    def test_friend_reaches_exclusion_zones_but_not_others(self):
        self.share(self.owner, "Share_Friend")
        self.assertEqual(self.as_user(self.friend).get(f"/telescopes/{self.scope.id}/zones").status_code, 200)
        self.assertEqual(self.as_user(self.stranger).get(f"/telescopes/{self.scope.id}/zones").status_code, 404)


if __name__ == "__main__":
    unittest.main()
