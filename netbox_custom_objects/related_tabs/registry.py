import logging

from netbox_custom_objects.constants import APP_LABEL

logger = logging.getLogger('netbox_custom_objects.related_tabs')

# Action name / path / URL name for the per-type tab route on custom-object host
# pages. Kept in sync with the {% custom_objects_tab_link %} <li>s in
# customobject.html and the custom_objects_tab_link template tag.
_CO_TYPED_ACTION = 'custom_objects'
_CO_TYPED_PATH = 'custom-objects'
# CustomObject._get_viewname('custom_objects') ->
# 'plugins:netbox_custom_objects:customobject_custom_objects'
CO_TYPED_URL_NAME = f'customobject_{_CO_TYPED_ACTION}'


def _inject_co_urls():
    """
    Inject the generic per-type tab URL for custom-object host pages into
    ``netbox_custom_objects.urls``.

    Custom-object detail pages are served by one generic view and never call
    ``get_model_urls()``, so typed tab views have no URL pattern. Add ONE
    slug-agnostic route
    (``<str:custom_object_type>/<int:pk>/custom-objects/<str:target_slug>/``)
    at ready() time, before the URLconf freezes — it reverses for ANY host/
    target pair, including COTs created after startup. The URL name follows
    CustomObject._get_viewname():
    ``plugins:netbox_custom_objects:customobject_custom_objects``.
    """
    try:
        import netbox_custom_objects.urls as co_urls
        from django.urls import path as url_path
    except ImportError:
        return

    existing_names = {p.name for p in co_urls.urlpatterns if hasattr(p, 'name') and p.name}
    if CO_TYPED_URL_NAME in existing_names:
        return

    from .views.typed import _make_co_typed_view

    full_path = f'<str:custom_object_type>/<int:pk>/{_CO_TYPED_PATH}/<str:target_slug>/'
    co_urls.urlpatterns.append(
        url_path(full_path, _make_co_typed_view().as_view(), name=CO_TYPED_URL_NAME)
    )
    logger.debug("injected URL pattern '%s'", CO_TYPED_URL_NAME)


def _typed_registration_pairs():
    """
    Enumerate the (host model, CustomObjectType) pairs that need a typed tab on
    a *built-in* model's detail page: every OBJECT/MULTIOBJECT field whose
    target is a built-in model.  CO→CO targets are excluded — those tabs are
    served by the generic injected URL and the live
    ``custom_objects_tab_link`` template tag.

    A pair's tab is registered at startup from the fields that exist then; a
    pair created later (a new type, or a type's first field pointing at a
    model) needs a web restart to get its tab — rows, counts, and columns are
    live regardless.
    """
    from django.db.utils import OperationalError, ProgrammingError

    from netbox_custom_objects.models import CustomObjectTypeField

    from .views.typed import _TYPE_CHOICES

    try:
        fields = list(
            CustomObjectTypeField.objects.filter(type__in=_TYPE_CHOICES).select_related(
                'custom_object_type', 'related_object_type'
            )
        )
    except (OperationalError, ProgrammingError):
        logger.warning('database unavailable — typed tabs not registered until next start')
        return []

    pairs = {}
    for field in fields:
        if field.is_polymorphic:
            targets = list(field.related_object_types.all())
        elif field.related_object_type_id:
            targets = [field.related_object_type]
        else:
            targets = []
        for ct in targets:
            if ct.app_label == APP_LABEL:
                continue  # CO→CO targets are served by the generic injected URL
            try:
                model = ct.model_class()
            except Exception:
                logger.exception(
                    'skipping ObjectType pk=%s (%s.%s): error resolving its model class',
                    ct.pk, ct.app_label, ct.model,
                )
                continue
            if model is None:
                logger.warning(
                    'skipping ObjectType pk=%s (%s.%s): no installed model — likely a stale row from an '
                    'uninstalled plugin or a deleted Custom Object Type',
                    ct.pk, ct.app_label, ct.model,
                )
                continue
            pairs.setdefault((model, field.custom_object_type), []).append(field)

    return pairs


def register_tabs():
    """
    Register per-CustomObjectType tabs ("Management Accesses", …) — the only
    related-object tab mode.

    Called from ``CustomObjectsPluginConfig.ready()`` as a third pass, after the
    existing two-pass model + serializer registration.  Two host kinds:

    * **Built-in NetBox models** — one tab per (host model, CustomObjectType)
      pair, enumerated from the fields that exist at startup.  The tab shows
      only when its live badge is non-zero (``hide_if_empty``).  A pair created
      after startup needs a web restart to get its tab (the URLconf freezes
      after ready()); rows, counts, and columns are always live.

    * **Custom-object host pages (CO→CO)** — served by a single generic URL
      injected here (``_inject_co_urls``) plus the live
      ``custom_objects_tab_link`` template tag.

    All registration must happen synchronously here: NetBox builds each model's
    URLconf (via ``get_model_urls()``) on the first ``resolve()`` call,
    snapshotting ``registry['views']`` at that moment; anything added later has
    no URL pattern.  Likewise, ``_inject_co_urls()`` mutates
    ``netbox_custom_objects.urls.urlpatterns`` and must run before the URL
    resolver populates its lookup cache against that list.
    """
    from django.urls import clear_url_caches

    try:
        # Inject the generic custom-object typed-tab URL first and
        # unconditionally.  It is a single slug-agnostic route, so it must
        # exist at startup (the URLconf freezes after ready()) to serve typed
        # tabs on custom-object host pages — including COTs created later
        # (CO→CO references).
        _inject_co_urls()

        from .views.typed import register_builtin_typed_tab

        for (host_model, cot), _fields in _typed_registration_pairs().items():
            try:
                register_builtin_typed_tab(host_model, cot)
            except Exception:
                # One bad pair must not take down the rest (ready() also
                # guards, but per-pair isolation keeps a single stale row
                # from losing every other tab).
                logger.exception(
                    'failed to register typed tab for COT %s (pk=%s) on %s.%s',
                    cot, cot.pk, host_model._meta.app_label, host_model._meta.model_name,
                )
    finally:
        # Always drop URL-resolver caches once we've mutated urlpatterns / the
        # view registry — even if model enumeration raised partway through.  A
        # resolver cache built earlier in ready() (by other plugins) would
        # otherwise leave the injected CO→CO route unresolvable until a restart.
        clear_url_caches()
