"""
Message translation for a web app.

    translations = Translations()
    app.register_extension(translations)

makes `_()`, `gettext()`, `ngettext()` and `pgettext()` available in templates, in the
new style where variables are passed as keyword arguments. Python code calls the methods
of the same object. Without a catalog messages pass through untranslated, so an app can
mark its messages from the start and add catalogs later.
"""

import gettext as gettext_module

from .base_app import BaseWebApp


class Translations:
    def __init__(self, catalog: gettext_module.NullTranslations | None = None) -> None:
        self.catalog = catalog or gettext_module.NullTranslations()

    def init_app(self, app: BaseWebApp) -> None:
        app.jinja_env.add_extension("jinja2.ext.i18n")
        app.jinja_env.install_gettext_translations(self.catalog, newstyle=True)  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]

    def gettext(self, message: str, **variables: object) -> str:
        translated = self.catalog.gettext(message)
        return translated % variables if variables else translated

    def ngettext(self, singular: str, plural: str, num: int, **variables: object) -> str:
        variables.setdefault("num", num)
        return self.catalog.ngettext(singular, plural, num) % variables

    def pgettext(self, context: str, message: str, **variables: object) -> str:
        translated = self.catalog.pgettext(context, message)
        return translated % variables if variables else translated
