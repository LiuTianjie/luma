from __future__ import annotations

import asyncio
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

LAE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAE_ROOT / "apps" / "api" / "src"))
sys.path.insert(0, str(LAE_ROOT / "packages" / "python" / "lae-store" / "src"))

try:  # Optional outside migration/integration CI jobs.
    from alembic import command
    from alembic.config import Config
except ImportError:  # pragma: no cover - skip condition handles this
    command = None
    Config = None

from lae_store.auth import (  # noqa: E402
    AuthKeyRing,
    AuthPolicy,
    AuthRejected,
    DeployTokenConflict,
    DeployTokenNotFound,
    PostgresAuthStore,
    utcnow,
)
from lae_store.engine import create_postgres_engine, create_session_factory  # noqa: E402
from lae_store.ids import new_id  # noqa: E402
from lae_store.models import (  # noqa: E402
    AuthSession,
    DeployToken,
    EmailChallenge,
    PlanVersion,
    Subscription,
    Tenant,
    TenantMember,
    User,
)
from lae_store.tokens import (  # noqa: E402
    issue_email_challenge,
    verify_deploy_token,
)
from lae_api.auth_service import AuthService  # noqa: E402
from lae_api.app import CSRF_COOKIE, SESSION_COOKIE, create_app  # noqa: E402
from lae_api.email import RecordingEmailSender  # noqa: E402

DSN = os.environ.get("LAE_TEST_POSTGRES_DSN", "")
DDL_ALLOWED = os.environ.get("LAE_TEST_POSTGRES_ALLOW_DDL") == "1"


def _alembic_config() -> Config:
    assert Config is not None
    config = Config(str(LAE_ROOT / "migrations" / "alembic.ini"))
    config.set_main_option("script_location", str(LAE_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", DSN)
    return config


def _upgrade() -> None:
    assert command is not None
    command.upgrade(_alembic_config(), "head")


def _downgrade() -> None:
    assert command is not None
    command.downgrade(_alembic_config(), "base")


@unittest.skipUnless(
    DSN and DDL_ALLOWED and command is not None,
    "set LAE_TEST_POSTGRES_DSN and LAE_TEST_POSTGRES_ALLOW_DDL=1 for real PostgreSQL",
)
class AuthPostgreSQLIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not DSN.startswith("postgresql+asyncpg://"):
            self.fail("integration DSN must use postgresql+asyncpg")
        await asyncio.to_thread(_upgrade)
        self.engine = create_postgres_engine(DSN)
        self.sessions = create_session_factory(self.engine)
        self.key = b"integration-auth-key-material-32b"
        self.store = PostgresAuthStore(
            self.sessions,
            AuthKeyRing(current_version=1, keys={1: self.key}),
            policy=AuthPolicy(resend_interval=timedelta(seconds=10)),
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        await asyncio.to_thread(_downgrade)

    async def _insert_active_challenge(self, email: str) -> tuple[str, str, str]:
        challenge_id = new_id("emc")
        issued = issue_email_challenge(
            self.key, key_version=1, challenge_id=challenge_id
        )
        now = utcnow()
        async with self.sessions() as session:
            async with session.begin():
                session.add(
                    EmailChallenge(
                        id=challenge_id,
                        email=email,
                        purpose="register",
                        code_hash=issued.code_digest,
                        magic_token_hash=issued.magic_token_digest,
                        key_version=1,
                        attempts=0,
                        max_attempts=5,
                        expires_at=now + timedelta(minutes=10),
                        activated_at=now,
                    )
                )
        return challenge_id, issued.code, issued.magic_token

    async def _register(self, email: str):
        pending = await self.store.begin_challenge(
            email=email,
            purpose="register",
            request_ip="203.0.113.40",
            device_id=f"device-{email}",
        )
        assert pending is not None
        self.assertTrue(await self.store.activate_challenge(pending.id))
        return await self.store.complete_challenge(
            email=email,
            purpose="register",
            method="code",
            credential=pending.code,
            request_ip="203.0.113.40",
            user_agent="deploy-token-integration",
        )

    async def test_migration_registration_fences_atomicity_and_lockout(self) -> None:
        async with self.sessions() as session:
            tables = set(
                (
                    await session.execute(
                        select(PlanVersion.code, PlanVersion.version)
                    )
                ).all()
            )
        self.assertIn(("lite", 1), tables)

        # Pending rows are unusable until the email adapter succeeds and the
        # service explicitly crosses the activated_at fence.
        pending = await self.store.begin_challenge(
            email="first@example.test",
            purpose="register",
            request_ip="203.0.113.1",
            device_id="device-a",
        )
        assert pending is not None
        with self.assertRaises(AuthRejected):
            await self.store.complete_challenge(
                email=pending.email,
                purpose="register",
                method="code",
                credential=pending.code,
                request_ip="203.0.113.1",
                user_agent="integration-test",
            )
        self.assertTrue(await self.store.activate_challenge(pending.id))

        # SELECT FOR UPDATE + used_at makes one challenge atomically consumable
        # once even when two API workers verify it concurrently.
        outcomes = await asyncio.gather(
            *[
                self.store.complete_challenge(
                    email=pending.email,
                    purpose="register",
                    method="code",
                    credential=pending.code,
                    request_ip="203.0.113.1",
                    user_agent="integration-test",
                )
                for _ in range(2)
            ],
            return_exceptions=True,
        )
        successes = [item for item in outcomes if not isinstance(item, Exception)]
        failures = [item for item in outcomes if isinstance(item, AuthRejected)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        first = successes[0]
        self.assertEqual(first.entitlement_code, "lite")
        self.assertIsNotNone(first.default_deploy_token)

        async with self.sessions() as session:
            user_count = await session.scalar(
                select(func.count(User.id)).where(
                    func.lower(User.email) == "first@example.test"
                )
            )
            tenant_count = await session.scalar(
                select(func.count(Tenant.id)).where(Tenant.owner_user_id == first.user_id)
            )
            member_count = await session.scalar(
                select(func.count()).select_from(TenantMember).where(
                    TenantMember.user_id == first.user_id
                )
            )
            subscription = await session.scalar(
                select(Subscription).where(Subscription.tenant_id == first.tenant_id)
            )
            deploy = await session.scalar(
                select(DeployToken).where(
                    DeployToken.tenant_id == first.tenant_id,
                    DeployToken.is_default.is_(True),
                )
            )
            auth_session = await session.scalar(
                select(AuthSession).where(AuthSession.id == first.session_id)
            )
        self.assertEqual((user_count, tenant_count, member_count), (1, 1, 1))
        self.assertIsNotNone(subscription)
        self.assertIsNotNone(deploy)
        self.assertIsNotNone(auth_session)
        assert deploy is not None
        assert auth_session is not None
        assert first.default_deploy_token is not None
        self.assertTrue(
            verify_deploy_token(
                first.default_deploy_token,
                expected_digest=deploy.token_hash,
                key=self.key,
            )
        )
        self.assertNotIn(first.default_deploy_token.encode(), deploy.token_hash)
        self.assertEqual(auth_session.key_version, 1)
        self.assertIsNotNone(auth_session.csrf_hash)
        principal = await self.store.authenticate(first.session_token)
        self.assertTrue(self.store.csrf_matches(principal, first.csrf_token))

        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(EmailChallenge)
                    .where(EmailChallenge.id == pending.id)
                    .values(created_at=func.now() - timedelta(seconds=11))
                )
        login_challenge = await self.store.begin_challenge(
            email="first@example.test",
            purpose="login",
            request_ip="203.0.113.3",
            device_id="device-login",
        )
        assert login_challenge is not None
        self.assertTrue(await self.store.activate_challenge(login_challenge.id))
        login = await self.store.complete_challenge(
            email="first@example.test",
            purpose="login",
            method="magic",
            credential=login_challenge.magic_token,
            request_ip="203.0.113.3",
            user_agent="integration-login",
        )
        self.assertEqual(login.user_id, first.user_id)
        self.assertEqual(login.tenant_id, first.tenant_id)
        self.assertIsNone(login.default_deploy_token)
        self.assertNotEqual(login.session_id, first.session_id)

        # Even if two independently issued registration challenges survive a
        # prior release/race, advisory locking makes onboarding one user,
        # personal tenant, Lite subscription and active default token.
        race_email = "race@example.test"
        _id_a, code_a, _magic_a = await self._insert_active_challenge(race_email)
        _id_b, code_b, _magic_b = await self._insert_active_challenge(race_email)
        race_results = await asyncio.gather(
            self.store.complete_challenge(
                email=race_email,
                purpose="register",
                method="code",
                credential=code_b,
                request_ip=None,
                user_agent=None,
            ),
            self.store.complete_challenge(
                email=race_email,
                purpose="register",
                method="magic",
                credential=_magic_a,
                request_ip=None,
                user_agent=None,
            ),
        )
        self.assertEqual(len({item.user_id for item in race_results}), 1)
        self.assertEqual(len({item.tenant_id for item in race_results}), 1)
        self.assertEqual(
            sum(item.default_deploy_token is not None for item in race_results), 1
        )
        race_user_id = race_results[0].user_id
        race_tenant_id = race_results[0].tenant_id
        async with self.sessions() as session:
            counts = (
                await session.scalar(
                    select(func.count(User.id)).where(
                        func.lower(User.email) == race_email
                    )
                ),
                await session.scalar(
                    select(func.count(Tenant.id)).where(
                        Tenant.owner_user_id == race_user_id
                    )
                ),
                await session.scalar(
                    select(func.count(Subscription.id)).where(
                        Subscription.tenant_id == race_tenant_id,
                        Subscription.status == "active",
                    )
                ),
                await session.scalar(
                    select(func.count(DeployToken.id)).where(
                        DeployToken.tenant_id == race_tenant_id,
                        DeployToken.is_default.is_(True),
                        DeployToken.revoked_at.is_(None),
                    )
                ),
            )
        self.assertEqual(counts, (1, 1, 1, 1))

        # Invalid attempts persist under the row lock; the fifth failure closes
        # the challenge and the correct credential cannot revive it.
        lock_email = "lockout@example.test"
        lock_id, lock_code, _lock_magic = await self._insert_active_challenge(lock_email)
        wrong_code = "000000" if lock_code != "000000" else "999999"
        for _ in range(5):
            with self.assertRaises(AuthRejected):
                await self.store.complete_challenge(
                    email=lock_email,
                    purpose="register",
                    method="code",
                    credential=wrong_code,
                    request_ip=None,
                    user_agent=None,
                )
        with self.assertRaises(AuthRejected):
            await self.store.complete_challenge(
                email=lock_email,
                purpose="register",
                method="code",
                credential=lock_code,
                request_ip=None,
                user_agent=None,
            )
        async with self.sessions() as session:
            locked = await session.get(EmailChallenge, lock_id)
        assert locked is not None
        self.assertEqual(locked.attempts, 5)
        self.assertIsNotNone(locked.canceled_at)

        expired_email = "expired@example.test"
        expired_id, expired_code, _expired_magic = await self._insert_active_challenge(
            expired_email
        )
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(EmailChallenge)
                    .where(EmailChallenge.id == expired_id)
                    .values(expires_at=func.now() - timedelta(seconds=1))
                )
        with self.assertRaises(AuthRejected):
            await self.store.complete_challenge(
                email=expired_email,
                purpose="register",
                method="code",
                credential=expired_code,
                request_ip=None,
                user_agent=None,
            )

        rate_store = PostgresAuthStore(
            self.sessions,
            AuthKeyRing(current_version=1, keys={1: self.key}),
            policy=AuthPolicy(
                email_starts_per_window=1,
                ip_starts_per_window=1,
                device_starts_per_window=1,
                resend_interval=timedelta(seconds=10),
            ),
        )
        allowed = await rate_store.begin_challenge(
            email="rate-a@example.test",
            purpose="register",
            request_ip="198.51.100.200",
            device_id="rate-device",
        )
        self.assertIsNotNone(allowed)
        self.assertIsNone(
            await rate_store.begin_challenge(
                email="rate-a@example.test",
                purpose="register",
                request_ip="198.51.100.201",
                device_id="rate-device-b",
            )
        )
        self.assertIsNone(
            await rate_store.begin_challenge(
                email="rate-b@example.test",
                purpose="register",
                request_ip="198.51.100.200",
                device_id="rate-device-c",
            )
        )
        self.assertIsNone(
            await rate_store.begin_challenge(
                email="rate-c@example.test",
                purpose="register",
                request_ip="198.51.100.202",
                device_id="rate-device",
            )
        )

        # Crash after pending commit but before send leaves no usable challenge.
        # It counts toward abuse limits only until the deterministic resend
        # interval; after that, a new request cancels/replaces it.
        crash = await self.store.begin_challenge(
            email="crash@example.test",
            purpose="register",
            request_ip="203.0.113.2",
            device_id=None,
        )
        assert crash is not None
        immediate = await self.store.begin_challenge(
            email="crash@example.test",
            purpose="register",
            request_ip="203.0.113.2",
            device_id=None,
        )
        self.assertIsNone(immediate)
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(EmailChallenge)
                    .where(EmailChallenge.id == crash.id)
                    .values(created_at=func.now() - timedelta(seconds=11))
                )
        replacement = await self.store.begin_challenge(
            email="crash@example.test",
            purpose="register",
            request_ip="203.0.113.2",
            device_id=None,
        )
        self.assertIsNotNone(replacement)
        async with self.sessions() as session:
            old = await session.get(EmailChallenge, crash.id)
        assert old is not None
        self.assertIsNone(old.activated_at)
        self.assertIsNotNone(old.canceled_at)

        sender = RecordingEmailSender(fail_next=True)
        service = AuthService(self.store, sender, minimum_start_duration=0)
        accepted = await service.start(
            email="delivery-failure@example.test",
            purpose="register",
            request_ip="203.0.113.4",
            device_id="delivery-device",
        )
        self.assertTrue(accepted.accepted)
        async with self.sessions() as session:
            delivery_failure = await session.scalar(
                select(EmailChallenge)
                .where(EmailChallenge.email == "delivery-failure@example.test")
                .order_by(EmailChallenge.created_at.desc())
                .limit(1)
            )
        assert delivery_failure is not None
        self.assertIsNone(delivery_failure.activated_at)
        self.assertIsNotNone(delivery_failure.canceled_at)

    async def test_auto_email_purpose_registers_new_and_logs_in_existing_user(self) -> None:
        email = "auto-auth@example.test"
        registration = await self.store.begin_challenge(
            email=email,
            purpose="auto",
            request_ip="203.0.113.50",
            device_id="auto-register-device",
        )
        assert registration is not None
        self.assertEqual(registration.purpose, "register")
        self.assertTrue(await self.store.activate_challenge(registration.id))
        registered = await self.store.complete_challenge(
            email=email,
            purpose="auto",
            method="code",
            credential=registration.code,
            request_ip="203.0.113.50",
            user_agent="auto-auth-integration",
        )
        self.assertIsNotNone(registered.default_deploy_token)

        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(EmailChallenge)
                    .where(EmailChallenge.email == email)
                    .values(created_at=func.now() - timedelta(seconds=11))
                )

        login = await self.store.begin_challenge(
            email=email,
            purpose="auto",
            request_ip="203.0.113.51",
            device_id="auto-login-device",
        )
        assert login is not None
        self.assertEqual(login.purpose, "login")
        self.assertTrue(await self.store.activate_challenge(login.id))
        logged_in = await self.store.complete_challenge(
            email=email,
            purpose="auto",
            method="magic",
            credential=login.magic_token,
            request_ip="203.0.113.51",
            user_agent="auto-auth-integration",
        )
        self.assertEqual(logged_in.user_id, registered.user_id)
        self.assertIsNone(logged_in.default_deploy_token)

    async def test_deploy_token_auth_rotation_scope_and_tenant_fences(self) -> None:
        first = await self._register("token-owner@example.test")
        assert first.default_deploy_token is not None
        session_principal = await self.store.authenticate(first.session_token)
        for forbidden_scope in ("billing:write", "tokens:write", "admin:*"):
            with self.assertRaises(ValueError):
                await self.store.create_deploy_token(
                    session_principal,
                    name="Forbidden",
                    scopes=(forbidden_scope,),
                    expires_at=None,
                )
        with self.assertRaises(ValueError):
            await self.store.create_deploy_token(
                session_principal,
                name=f"copied {first.default_deploy_token}",
                scopes=("apps:read",),
                expires_at=None,
            )

        authenticated = await self.store.authenticate_deploy_token(
            first.default_deploy_token,
            request_ip="2001:db8::1",
        )
        self.assertEqual(authenticated.user_id, first.user_id)
        self.assertEqual(authenticated.tenant_id, first.tenant_id)
        self.assertIn("analyses:write", authenticated.scopes)
        self.assertIn("apps:write", authenticated.scopes)
        self.assertNotIn("billing:checkout", authenticated.scopes)
        async with self.sessions() as session:
            default_row = await session.scalar(
                select(DeployToken).where(
                    DeployToken.id == authenticated.token_id
                )
            )
        assert default_row is not None
        self.assertIsNotNone(default_row.last_used_at)
        self.assertEqual(str(default_row.last_used_ip), "2001:db8::1")

        wrong = first.default_deploy_token[:-1] + (
            "A" if first.default_deploy_token[-1] != "A" else "B"
        )
        for rejected in (wrong, "malformed"):
            with self.assertRaises(AuthRejected):
                await self.store.authenticate_deploy_token(
                    rejected,
                    request_ip="203.0.113.41",
                )

        key_v2 = b"integration-auth-key-version-two"
        rotated_key_store = PostgresAuthStore(
            self.sessions,
            AuthKeyRing(current_version=2, keys={1: self.key, 2: key_v2}),
        )
        # Keeping the old key verifies existing tokens after an HMAC key-ring
        # rotation, while newly issued credentials are bound to version 2.
        old_after_key_rotation = await rotated_key_store.authenticate_deploy_token(
            first.default_deploy_token,
            request_ip=None,
        )
        self.assertEqual(old_after_key_rotation.token_id, default_row.id)
        limited = await rotated_key_store.create_deploy_token(
            session_principal,
            name="Read-only agent",
            scopes=("apps:read", "logs:read"),
            expires_at=None,
        )
        self.assertNotIn(limited.plaintext, repr(limited))
        limited_principal = await rotated_key_store.authenticate_deploy_token(
            limited.plaintext,
            request_ip="198.51.100.9",
        )
        self.assertEqual(
            limited_principal.scopes,
            frozenset({"apps:read", "logs:read"}),
        )
        checkout = await rotated_key_store.create_deploy_token(
            session_principal,
            name="Checkout agent",
            scopes=("billing:checkout",),
            expires_at=None,
        )
        checkout_principal = await rotated_key_store.authenticate_deploy_token(
            checkout.plaintext,
            request_ip=None,
        )
        self.assertEqual(
            checkout_principal.scopes,
            frozenset({"billing:checkout"}),
        )
        async with self.sessions() as session:
            limited_row = await session.get(DeployToken, limited.record.id)
        assert limited_row is not None
        self.assertEqual(limited_row.key_version, 2)
        with self.assertRaises(AuthRejected):
            await self.store.authenticate_deploy_token(
                limited.plaintext,
                request_ip=None,
            )

        revokable = await rotated_key_store.create_deploy_token(
            session_principal,
            name="Revokable",
            scopes=("deployments:write",),
            expires_at=None,
        )
        await rotated_key_store.revoke_deploy_token(
            session_principal, revokable.record.id
        )
        with self.assertRaises(AuthRejected):
            await rotated_key_store.authenticate_deploy_token(
                revokable.plaintext,
                request_ip=None,
            )

        expiring = await rotated_key_store.create_deploy_token(
            session_principal,
            name="Expiring",
            scopes=("analyses:write",),
            expires_at=utcnow() + timedelta(minutes=5),
        )
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(DeployToken)
                    .where(DeployToken.id == expiring.record.id)
                    .values(expires_at=func.now() - timedelta(seconds=1))
                )
        with self.assertRaises(AuthRejected):
            await rotated_key_store.authenticate_deploy_token(
                expiring.plaintext,
                request_ip=None,
            )

        concurrent = await rotated_key_store.create_deploy_token(
            session_principal,
            name="Concurrent rotate",
            scopes=("apps:read", "apps:write"),
            expires_at=None,
        )
        outcomes = await asyncio.gather(
            rotated_key_store.rotate_deploy_token(
                session_principal, concurrent.record.id
            ),
            rotated_key_store.rotate_deploy_token(
                session_principal, concurrent.record.id
            ),
            return_exceptions=True,
        )
        self.assertEqual(
            sum(not isinstance(outcome, Exception) for outcome in outcomes), 1
        )
        self.assertEqual(
            sum(isinstance(outcome, DeployTokenConflict) for outcome in outcomes), 1
        )
        with self.assertRaises(DeployTokenConflict) as protected:
            await rotated_key_store.revoke_deploy_token(
                session_principal, default_row.id
            )
        self.assertEqual(protected.exception.reason, "default_protected")

        second = await self._register("other-token-owner@example.test")
        second_principal = await self.store.authenticate(second.session_token)
        with self.assertRaises(DeployTokenNotFound):
            await rotated_key_store.rotate_deploy_token(
                second_principal, limited.record.id
            )
        second_tokens = await rotated_key_store.list_deploy_tokens(second_principal)
        self.assertNotIn(limited.record.id, {token.id for token in second_tokens})

        api = create_app(
            AuthService(
                rotated_key_store,
                RecordingEmailSender(),
                minimum_start_duration=0,
            )
        )
        transport = ASGITransport(app=api)
        async with AsyncClient(
            transport=transport,
            base_url="https://lae.example.test",
        ) as bearer_client:
            bearer_me = await bearer_client.get(
                "/v1/me",
                headers={"Authorization": f"Bearer {limited.plaintext}"},
            )
            self.assertEqual(bearer_me.status_code, 200)
            self.assertEqual(
                bearer_me.json()["credential"]["type"], "deploy_token"
            )
            bearer_mutation = await bearer_client.post(
                "/v1/deploy-tokens",
                headers={"Authorization": f"Bearer {limited.plaintext}"},
                json={"name": "Not allowed", "scopes": ["apps:read"]},
            )
            self.assertEqual(bearer_mutation.status_code, 401)

        async with AsyncClient(
            transport=transport,
            base_url="https://lae.example.test",
        ) as session_client:
            session_client.cookies.set(SESSION_COOKIE, first.session_token)
            session_client.cookies.set(CSRF_COOKIE, first.csrf_token)
            without_csrf = await session_client.post(
                "/v1/deploy-tokens",
                json={"name": "API agent", "scopes": ["apps:read"]},
            )
            self.assertEqual(without_csrf.status_code, 403)
            created = await session_client.post(
                "/v1/deploy-tokens",
                headers={"X-CSRF-Token": first.csrf_token},
                json={"name": "API agent", "scopes": ["apps:read"]},
            )
            self.assertEqual(created.status_code, 201)
            api_plaintext = created.json()["plaintext"]
            listed = await session_client.get("/v1/deploy-tokens")
            self.assertEqual(listed.status_code, 200)
            self.assertNotIn(api_plaintext, listed.text)
            assert second.default_deploy_token is not None
            confused = await session_client.get(
                "/v1/me",
                headers={
                    "Authorization": f"Bearer {second.default_deploy_token}"
                },
            )
            self.assertEqual(confused.status_code, 401)

        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(User)
                    .where(User.id == first.user_id)
                    .values(status="suspended")
                )
        with self.assertRaises(AuthRejected):
            await rotated_key_store.authenticate_deploy_token(
                first.default_deploy_token,
                request_ip=None,
            )
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(User)
                    .where(User.id == first.user_id)
                    .values(status="active")
                )
                await session.execute(
                    update(Tenant)
                    .where(Tenant.id == first.tenant_id)
                    .values(status="suspended")
                )
        with self.assertRaises(AuthRejected):
            await rotated_key_store.authenticate_deploy_token(
                first.default_deploy_token,
                request_ip=None,
            )
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(Tenant)
                    .where(Tenant.id == first.tenant_id)
                    .values(status="active")
                )

        # Entitlement state is part of token authentication, not merely UI
        # metadata. A canceled subscription invalidates an otherwise valid HMAC.
        async with self.sessions() as session:
            async with session.begin():
                await session.execute(
                    update(Subscription)
                    .where(Subscription.tenant_id == first.tenant_id)
                    .values(status="canceled")
                )
        with self.assertRaises(AuthRejected):
            await rotated_key_store.authenticate_deploy_token(
                first.default_deploy_token,
                request_ip=None,
            )


if __name__ == "__main__":
    unittest.main()
