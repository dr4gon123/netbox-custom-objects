"""
Per-CustomObjectType related-object tabs (the only tab mode).

For every CustomObjectType field that targets a model, the host model's
detail page gets ONE TAB PER REFERENCING TYPE — labeled with the type's
plural name, badged with the rows the viewer may see, hidden when empty
— rendering the type's real column set (the same dynamic table as the
type's list view: field columns, per-row actions, column preferences),
filtered in SQL to the rows referencing the host object.

Two host kinds:

* **Built-in NetBox models** (Device, Site, …) — one tab view is
  registered per (host model, CustomObjectType) pair at startup, from the
  fields that exist then. A pair created *later* (a new type, or a type's
  first field pointing at a model) needs a web restart to get its tab —
  the URLconf freezes after ``ready()`` and NetBox's registry-driven tab
  nav has no per-render hook on models whose templates we don't own.
  Everything else is live: rows, counts, and columns resolve per request.

* **Custom-object host pages** (a COT field that targets another COT —
  CO→CO) — the plugin owns ``customobject.html``, so the per-type nav
  links are rendered live by the ``custom_objects_tab_link`` template tag
  (computed from the DB per render), and their URL is a single generic
  route injected once at startup (``registry._inject_co_urls``) that
  reverses for *any* host/target slug pair, including COTs created after
  startup. CO→CO tabs appear with **no restart**.

This replaces the former single combined "Custom Objects" tab (and its
``tab_label`` / ``max_multiobject_display`` settings), which listed
references generically and could not show the referenced rows' content.
"""

import logging
from functools import reduce
from operator import or_

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.paginator import InvalidPage
from django.db.models import Q
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext_lazy as _
from django.views.generic import View
from extras.choices import CustomFieldUIVisibleChoices, CustomFieldTypeChoices
from netbox.context import current_request
from netbox.views import generic
from netbox_custom_objects.constants import APP_LABEL
from netbox_custom_objects.field_types import FIELD_TYPE_CLASS
from netbox_custom_objects.models import CustomObjectType
from netbox_custom_objects.tables import CustomObjectTable
from utilities.htmx import htmx_partial
from utilities.paginator import EnhancedPaginator, get_paginate_count
from utilities.views import ConditionalLoginRequiredMixin, ViewTab, get_default_template, register_model_view

logger = logging.getLogger('netbox_custom_objects.related_tabs')

# Tail of the tab bar: content tabs are related-object detail, not the
# object's primary identity.
TYPED_WEIGHT = 2000

_TYPE_CHOICES = (CustomFieldTypeChoices.TYPE_OBJECT, CustomFieldTypeChoices.TYPE_MULTIOBJECT)


def reference_q(host_ct_id, host_pk, field_name, field_type, is_polymorphic, through_model_name=None):
    """
    Build a Q selecting custom-object rows whose ``field_name`` references the host
    object identified by (``host_ct_id``, ``host_pk``).  Single source of truth for
    the four reference shapes the typed tab view filters on:

      * OBJECT, non-polymorphic      -> ``{name}_id``
      * OBJECT, polymorphic          -> ``{name}_content_type_id`` + ``{name}_object_id``
      * MULTIOBJECT, non-polymorphic -> ``{name}`` (reverse M2M)
      * MULTIOBJECT, polymorphic     -> ``pk__in`` subquery over the field's through table

    Returns an EMPTY ``Q()`` for an unsupported field type or an unresolvable
    polymorphic through model.  Callers MUST treat an empty Q as "matches nothing /
    skip" and never pass it to ``.filter()`` directly — ``filter(Q())`` matches
    every row (an empty Q is the identity element for ``|``).
    """
    if field_type == CustomFieldTypeChoices.TYPE_OBJECT:
        if is_polymorphic:
            return Q(**{f'{field_name}_content_type_id': host_ct_id, f'{field_name}_object_id': host_pk})
        return Q(**{f'{field_name}_id': host_pk})

    if field_type == CustomFieldTypeChoices.TYPE_MULTIOBJECT:
        if is_polymorphic:
            try:
                through = apps.get_model(APP_LABEL, through_model_name)
            except LookupError:
                logger.exception(
                    'Could not resolve through model %r for polymorphic field %s', through_model_name, field_name
                )
                return Q()
            return Q(pk__in=through.objects.filter(content_type_id=host_ct_id, object_id=host_pk).values('source_id'))
        return Q(**{field_name: host_pk})

    return Q()


def _restrict_or_warn(qs, user, *, label):
    """
    Apply NetBox's per-row ``.restrict(user, 'view')`` to ``qs``.

    If the queryset's manager doesn't implement ``.restrict()`` (rare — only
    models whose manager isn't a RestrictedQuerySet), log a warning and return
    ``qs`` unrestricted, so a silent permission bypass is observable in logs
    rather than invisible.
    """
    try:
        return qs.restrict(user, 'view')
    except AttributeError:
        logger.warning('%s lacks restrict(user, view); per-row permission filter skipped', label)
        return qs


def _fields_referencing(cot, host_ct):
    """
    The COT's OBJECT/MULTIOBJECT fields that reference ``host_ct`` — resolved
    live on every request, so a field added to an existing (type, host) pair
    shows up without a restart.  Only NEW pairs need one (see module docstring).
    """
    fields = []
    for f in cot.fields.filter(type__in=_TYPE_CHOICES):
        if f.is_polymorphic:
            if f.related_object_types.filter(pk=host_ct.pk).exists():
                fields.append(f)
        elif f.related_object_type_id == host_ct.pk:
            fields.append(f)
    return fields


def _typed_queryset(cot, instance, user=None):
    """
    Rows of ``cot`` referencing ``instance`` via any of its referencing fields,
    permission-filtered.  Returns None when the type has no referencing fields
    for the instance's content type (callers MUST skip None, never filter).
    """
    host_ct = ContentType.objects.get_for_model(instance._meta.model)
    fields = _fields_referencing(cot, host_ct)
    if not fields:
        return None
    queries = [
        reference_q(host_ct.pk, instance.pk, f.name, f.type, f.is_polymorphic, f.through_model_name)
        for f in fields
    ]
    q = reduce(or_, (q for q in queries if q.children), Q())
    if not q.children:
        return None
    queryset = cot.get_model().objects.filter(q)
    if user is not None:
        queryset = _restrict_or_warn(queryset, user, label=queryset.model._meta.label)
    return queryset


def _tab_label(cot):
    """The tab label for a CustomObjectType: plural name, with fallbacks."""
    return cot.verbose_name_plural or cot.verbose_name or cot.name


def _make_badge(cot):
    """
    Badge callable for the per-type ViewTab: the rows of ``cot`` referencing the
    host instance that the viewing user may see (so the badge matches the rows
    shown and ``hide_if_empty`` hides the tab when that is none).  The user is
    read from NetBox's ``current_request`` ContextVar; with no request context
    (shell, background jobs) the count falls back to unrestricted.
    """

    def _count(instance):
        user = getattr(current_request.get(), 'user', None)
        qs = _typed_queryset(cot, instance, user)
        if qs is None:
            return None
        return qs.count() or None

    return _count


def build_table_class(cot):
    """
    The type's real table: one column per visible field (same construction as
    the type's list view), on the shared CustomObjectTable base (pk toggle,
    per-row edit/delete actions, tags).  Built per request so column changes
    are live without a restart; the table name matches the list view's, so
    column preferences carry over between the two.
    """
    model = cot.get_model()
    visible = [f for f in cot.fields.all() if f.ui_visible != CustomFieldUIVisibleChoices.HIDDEN]
    fields = ['id'] + [f.name for f in visible]

    meta = type(
        'Meta',
        (),
        {
            'model': model,
            'fields': fields,
            'attrs': {
                'class': 'table table-hover object-list',
            },
        },
    )

    attrs = {
        'Meta': meta,
        '__module__': 'database.tables',
    }

    for field in visible:
        field_type = FIELD_TYPE_CLASS[field.type]()
        try:
            attrs[field.name] = field_type.get_table_column_field(field)
        except NotImplementedError:
            logger.debug('typed tab: %s field is not implemented; using a default column', field.name)
        # Primary field (if text-based) is linkified to the target Custom Object. Other fields may be
        # rendered via field-specific "render_foo" methods as supported by django-tables2.
        linkable_field_types = [
            CustomFieldTypeChoices.TYPE_TEXT,
            CustomFieldTypeChoices.TYPE_LONGTEXT,
        ]
        if field.primary and field.type in linkable_field_types:
            attrs[f'render_{field.name}'] = field_type.render_table_column_linkified
        else:
            try:
                attrs[f'render_{field.name}'] = field_type.render_table_column
            except AttributeError:
                pass

    return type(
        f'{model._meta.object_name}Table',
        (CustomObjectTable,),
        attrs,
    )


class DynamicTableMixin:
    """
    Adapter so typed views reuse the plugin's dynamic-table construction
    (``build_table_class``) through the same ``get_table()`` hook NetBox's
    generic views call.  ``self.custom_object_type`` must be set before
    ``get_table()`` runs.
    """

    def get_table(self, data, request, bulk_actions=True):
        self.table = build_table_class(self.custom_object_type)
        return super().get_table(data, request, bulk_actions=bulk_actions)


def _paginate(table, request):
    """Paginate ``table`` per the request (per-page param), NetBox-style."""
    paginator = EnhancedPaginator(table.data, get_paginate_count(request))
    try:
        page = paginator.page(int(request.GET.get('page', 1)))
    except (InvalidPage, ValueError):
        page = paginator.page(1)
    table.paginator = paginator
    table.page = page


def _make_typed_tab_view(host_model, cot):
    """
    Factory returning a View subclass for one (host model, CustomObjectType)
    pair.  Each pair gets its own class so NetBox's view registry stores
    separate entries and URL names do not collide.

    Dispatch enforces the PARENT's view permission (the queryset is the host
    model); the child rows are additionally restricted per user in
    ``get_children()``.
    """

    class _TypedTabView(DynamicTableMixin, generic.ObjectChildrenView):
        queryset = host_model.objects.all()
        child_model = cot.get_model()
        tab = ViewTab(
            label=_tab_label(cot),
            badge=_make_badge(cot),
            weight=TYPED_WEIGHT,
            hide_if_empty=True,
        )

        def get_children(self, request, parent):
            return _typed_queryset(cot, parent, request.user)

    _TypedTabView.__name__ = f'{host_model.__name__}_{cot.slug}_TypedTabView'
    _TypedTabView.__qualname__ = _TypedTabView.__name__
    return _TypedTabView


def _make_co_typed_view():
    """
    The per-type tab view for *custom-object* host pages (CO→CO).

    One view serves every (host, target) slug pair — its URL is the generic
    route injected by ``registry._inject_co_urls`` and the nav links are
    rendered live by the ``custom_objects_tab_link`` template tag, so types
    created after startup get their tabs without a restart.  Deliberately a
    plain View (not ObjectChildrenView): the parent is resolved from the host
    slug's dynamic model, so ObjectView's queryset-based permission check
    can't apply; rows are restricted manually instead, mirroring how the
    former combined CO view worked.
    """
    from netbox.views.generic.mixins import TableMixin

    class _COTypedTabView(ConditionalLoginRequiredMixin, DynamicTableMixin, TableMixin, View):
        tab = ViewTab(label=_('Custom Objects'), hide_if_empty=False)  # links come from the template tag

        def get(self, request, custom_object_type, pk, target_slug):
            host_cot = get_object_or_404(CustomObjectType, slug=custom_object_type)
            target_cot = get_object_or_404(CustomObjectType, slug=target_slug)
            host_model = host_cot.get_model()
            parent = get_object_or_404(
                _restrict_or_warn(host_model.objects.all(), request.user, label=host_model._meta.label),
                pk=pk,
            )

            self.custom_object_type = target_cot
            children = _typed_queryset(target_cot, parent, request.user)
            if children is None:
                children = target_cot.get_model().objects.none()
            table = self.get_table(children, request)
            _paginate(table, request)

            if htmx_partial(request):
                return render(request, 'htmx/table.html', {
                    'object': parent,
                    'table': table,
                    'model': target_cot.get_model(),
                })

            return render(request, 'generic/object_children.html', {
                'object': parent,
                'model': target_cot.get_model(),
                'child_model': target_cot.get_model(),
                'base_template': get_default_template(parent),
                'table': table,
                'table_config': f'{table.name}_config',
                'filter_form': None,
                'actions': (),
                'tab': self.tab,
                'return_url': request.get_full_path(),
            })

    return _COTypedTabView


def _register_tab_view(model_class, name, path, view_factory):
    """
    Register a model-view tab on ``model_class``, building it via ``view_factory``.

    Idempotent: if a tab with this ``name`` already exists for the model, log and
    skip — this guards against the Django autoreloader re-running registration.
    ``view_factory`` is a zero-arg callable so the view class isn't built on the
    already-registered path.

    Returns True if registered, False if skipped.
    """
    from netbox.registry import registry

    app_label = model_class._meta.app_label
    model_name = model_class._meta.model_name
    existing = registry['views'].get(app_label, {}).get(model_name, [])
    if any(entry['name'] == name for entry in existing):
        logger.debug('tab %r already registered for %s.%s — skipping', name, app_label, model_name)
        return False
    register_model_view(model_class, name=name, path=path)(view_factory())
    logger.debug('registered tab %r for %s.%s', name, app_label, model_name)
    return True


def register_builtin_typed_tab(host_model, cot):
    """
    Register the (host model, CustomObjectType) tab.  Public so tests can
    drive registration for individual pairs.
    """
    return _register_tab_view(
        host_model,
        f'custom_objects_{cot.slug}',
        f'custom-objects/{cot.slug}/',
        lambda hm=host_model, c=cot: _make_typed_tab_view(hm, c),
    )
