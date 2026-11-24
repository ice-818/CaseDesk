from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cli.client import ChatCli, ChatFailed
from app.cli.commands import build_parser
from app.core.time import utcnow
from app.main import create_app
from tests.auth_support import TEST_PASSWORD


@pytest.mark.parametrize("local", [True, False])
def test_cli_http_and_local_require_real_valid_credentials(local):
    with TestClient(create_app()) as client:

        async def exercise():
            cli = ChatCli(
                user_id="u_demo",
                stream=False,
                show_tokens=False,
                timeout=5,
                base_url="http://testserver",
                local=local,
            )
            if local:
                cli._container = client.app.state.container
            else:
                cli._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app))
            try:
                with pytest.raises(ChatFailed):
                    await cli.chat("你好")
                with pytest.raises(ChatFailed):
                    await cli.login("u_demo", "wrong")
                await cli.login("u_demo", TEST_PASSWORD)
                assert (await cli.chat("你好"))["answer"]
                cli._token = "forged"
                with pytest.raises(ChatFailed):
                    await cli.chat("你好")
                assert cli._token is None
                await cli.login("u_demo", TEST_PASSWORD)
                auth = client.app.state.container.auth_service
                auth.now = lambda: utcnow() + timedelta(days=9)
                with pytest.raises(ChatFailed):
                    await cli.chat("你好")
                auth.now = utcnow
                await cli.login("u_demo", TEST_PASSWORD)
                token = cli._token
                await auth.logout(await auth.authenticate(token))
                with pytest.raises(ChatFailed):
                    await cli.chat("你好")
                await cli.login("u_demo", TEST_PASSWORD)
                token = cli._token
                await cli.logout()
                assert cli._token is None
                with pytest.raises(Exception):
                    await auth.authenticate(token)
            finally:
                cli._container = None  # The enclosing TestClient owns this container.
                await cli.aclose()

        client.portal.call(exercise)


def test_cli_hidden_password_entry_and_no_anonymous_fallback(monkeypatch, capsys):
    with TestClient(create_app()) as client:

        async def exercise():
            cli = ChatCli(
                user_id="u_demo",
                stream=False,
                show_tokens=False,
                timeout=5,
                base_url="http://testserver",
                local=False,
            )

            async def unavailable():
                return False

            monkeypatch.setattr(cli, "_api_up", unavailable)
            with pytest.raises(ChatFailed, match="API 不可达"):
                await cli.setup()
            assert cli._container is None
            cli._container = client.app.state.container
            monkeypatch.setattr("app.cli.client.getpass.getpass", lambda _: TEST_PASSWORD)
            await cli._interactive_login()
            token = cli._token
            assert TEST_PASSWORD not in capsys.readouterr().out
            await cli.logout()
            cli._container = None
            return token

        token = client.portal.call(exercise)
        assert token not in capsys.readouterr().out
    assert build_parser().parse_args(["login", "--user-id", "u_demo"]).command == "login"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["login", "--password", "not-supported"])
