import pytest
from quart.typing import ResponseReturnValue

from scaffold.web.base_app import BaseWebApp
from scaffold.web.base_controller import BaseController
from scaffold.web.csrf import csrf_exempt, register_csrf
from scaffold.web.decorators import controller, route


@pytest.mark.asyncio
async def test_exempting_one_controller_method_does_not_exempt_a_same_named_one() -> None:
    # Two controllers in one module, each with a method named `update`. Exempting the
    # webhook's must not exempt the profile's: method names are not unique across controllers.
    @controller(name="hooks", url_prefix="/hooks")
    class HookController(BaseController):
        @route("/update", methods=["POST"])
        @csrf_exempt
        async def update(self) -> ResponseReturnValue:
            return "hook"

    @controller(name="profile", url_prefix="/profile")
    class ProfileController(BaseController):
        @route("/update", methods=["POST"])
        async def update(self) -> ResponseReturnValue:
            return "profile updated"

    app = BaseWebApp("__main__", secret_key=b"test-secret-key")
    register_csrf(app)
    app.register_controllers(
        {
            HookController: lambda: HookController(),
            ProfileController: lambda: ProfileController(),
        },
    )
    client = app.test_client()

    exempt = await client.post("/hooks/update")
    assert exempt.status_code == 200

    protected = await client.post("/profile/update")
    assert protected.status_code == 400
