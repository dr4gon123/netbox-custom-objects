from django import template
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.urls.exceptions import NoReverseMatch
from django.utils.module_loading import import_string
from netbox.registry import registry
from utilities.views import get_action_url

__all__ = ('plugin_extra_tabs', 'custom_objects_tab_link')

register = template.Library()

# journal/changelog/contacts/custom_objects are rendered as hardcoded <li>s in
# templates/netbox_custom_objects/customobject.html (keep this set in sync with it),
# not from the registry, so they're excluded here to avoid duplicate, never-active tabs:
# upstream's journal/changelog/contacts views set the active-tab marker as a string
# that ``model_view_tabs`` can't match (contacts is wired to the slug-based
# ``customobject_contacts`` route via NetBox's ContactsMixin), and custom_objects is
# rendered live by ``custom_objects_tab_link`` (so CO->CO tabs appear without a restart).
_HARDCODED_TAB_NAMES = frozenset({'journal', 'changelog', 'contacts', 'custom_objects'})


@register.inclusion_tag('tabs/model_view_tabs.html', takes_context=True)
def plugin_extra_tabs(context, instance):
    """
    Render registered model-view tabs for `instance`, excluding tabs that the
    Custom Object detail template already renders by hand (Journal, Changelog,
    and the per-type Custom Objects tabs — see _HARDCODED_TAB_NAMES).
    """
    app_label = instance._meta.app_label
    model_name = instance._meta.model_name
    user = context['request'].user
    tabs = []

    try:
        views = registry['views'][app_label][model_name]
    except KeyError:
        views = []

    for config in views:
        if config['name'] in _HARDCODED_TAB_NAMES:
            continue
        view = import_string(config['view']) if type(config['view']) is str else config['view']
        if tab := getattr(view, 'tab', None):
            if tab.permission and not user.has_perm(tab.permission):
                continue
            if attrs := tab.render(instance):
                try:
                    url = get_action_url(instance, action=config['name'], kwargs={'pk': instance.pk})
                except NoReverseMatch:
                    continue
                tabs.append(
                    {
                        'name': config['name'],
                        'url': url,
                        'label': attrs['label'],
                        'badge': attrs['badge'],
                        'weight': attrs['weight'],
                        'is_active': context.get('tab') == tab,
                    }
                )

    tabs = sorted(tabs, key=lambda x: x['weight'])
    return {'tabs': tabs}


@register.inclusion_tag('netbox_custom_objects/related_tabs/typed/tab_links.html', takes_context=True)
def custom_objects_tab_link(context, instance):
    """
    Render the per-type "Custom Objects" tab nav-links on a custom object
    detail page — one link per referencing CustomObjectType — computed live
    from the DB (not the startup view registry).

    This is what makes references *between* custom object types live without a
    NetBox restart: the tabs' URLs reverse a single slug-agnostic route
    injected at startup (``registry._inject_co_urls``), and each nav-link's
    visibility/badge are recomputed per render here.  A type with a zero badge
    renders no link (hide_if_empty), and nothing renders at all when the URL
    can't be reversed (plugin URLs not loaded).
    """
    from extras.choices import CustomFieldTypeChoices
    from netbox_custom_objects.models import CustomObjectTypeField
    from netbox_custom_objects.related_tabs.views.typed import _tab_label, _typed_queryset

    host_ct = ContentType.objects.get_for_model(instance._meta.model)
    type_choices = (CustomFieldTypeChoices.TYPE_OBJECT, CustomFieldTypeChoices.TYPE_MULTIOBJECT)
    field_list = CustomObjectTypeField.objects.filter(
        (Q(related_object_type=host_ct) & Q(is_polymorphic=False))
        | (Q(related_object_types=host_ct) & Q(is_polymorphic=True)),
        type__in=type_choices,
    ).select_related('custom_object_type')

    request = context.get('request')
    user = getattr(request, 'user', None)

    tabs = []
    seen = set()
    for field in field_list:
        target = field.custom_object_type
        if target.pk in seen:
            continue
        seen.add(target.pk)

        qs = _typed_queryset(target, instance, user)
        badge = qs.count() if qs is not None else 0
        if not badge:
            continue

        try:
            url = get_action_url(
                instance, action='custom_objects', kwargs={'pk': instance.pk, 'target_slug': target.slug}
            )
        except NoReverseMatch:
            continue

        # Active iff we are actually on that tab's page.  Compare the request
        # path to the tab URL rather than inspecting context['tab']: other
        # plugins may also register ViewTab-bearing views on custom-object
        # models, so a type-based check would light links up on those tabs too.
        tabs.append(
            {
                'url': url,
                'label': _tab_label(target),
                'badge': badge,
                'is_active': request is not None and request.path == url,
            }
        )

    tabs.sort(key=lambda t: str(t['label']).lower())
    return {'tabs': tabs}
