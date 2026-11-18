import gettext as gettext_module

import pytest
from quart import render_template_string

from scaffold.web import BaseWebApp
from scaffold.web.i18n import Translations


class Czech(gettext_module.NullTranslations):
    def gettext(self, message: str) -> str:
        return {"Hello %(name)s": "Ahoj %(name)s"}.get(message, message)

    def ngettext(self, msgid1: str, msgid2: str, n: int) -> str:
        return "%(num)s soubor" if n == 1 else "%(num)s soubory"


def test_messages_pass_through_without_a_catalog() -> None:
    translations = Translations()

    assert translations.gettext("Hello %(name)s", name="Eva") == "Hello Eva"
    assert translations.gettext("100% done") == "100% done"
    assert translations.ngettext("%(num)s file", "%(num)s files", 2) == "2 files"
    assert translations.pgettext("button", "Save") == "Save"


def test_messages_are_translated_with_a_catalog() -> None:
    translations = Translations(Czech())

    assert translations.gettext("Hello %(name)s", name="Eva") == "Ahoj Eva"
    assert translations.ngettext("%(num)s file", "%(num)s files", 1) == "1 soubor"


@pytest.mark.asyncio
async def test_templates_can_translate() -> None:
    app = BaseWebApp("__main__", serve_assets=False)
    app.register_extension(Translations(Czech()))

    async with app.app_context():
        rendered = await render_template_string(
            "{{ _('Hello %(name)s', name='Eva') }}|{{ ngettext('%(num)s file', '%(num)s files', 3) }}",
        )

    assert rendered == "Ahoj Eva|3 soubory"
