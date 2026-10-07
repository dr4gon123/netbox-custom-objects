"""
Tests for the related_tabs subpackage (per-CustomObjectType tabs).

Focused on the surfaces most likely to regress:

* ``reference_q()`` builds the correct filter per field kind, and returns an
  EMPTY Q (which callers must treat as "skip", never as match-all) for an
  unsupported field type or an unresolvable polymorphic through model. A
  regression here would leak every custom object of a type onto every host page.
* ``register_builtin_typed_tab()`` adds a ``custom_objects_<slug>`` view to
  NetBox's view registry for the (host model, type) pair, idempotently — one
  tab per type, not one shared tab.
* ``_typed_queryset()`` returns None when the type has no referencing fields
  for the host's content type, OR-merges multiple referencing fields of the
  same type (a row referencing the host via either field appears exactly
  once), and resolves the polymorphic MULTIOBJECT through table.
* The per-type badge counts only its own type's rows, returns None for an
  unreferenced host (hide_if_empty), and restricts per user.
* The ``custom_objects_tab_link`` tag renders one live link per referencing
  type on custom-object host pages (CO→CO), skipping zero-badge types.
"""

from types import SimpleNamespace

from core.models import ObjectType
from django.db.models import Q
from django.test import RequestFactory, TestCase, TransactionTestCase
from extras.choices import CustomFieldTypeChoices
from netbox.context import current_request
from netbox.registry import registry

from dcim.models import Site

from netbox_custom_objects.constants import APP_LABEL
from netbox_custom_objects.related_tabs.registry import register_tabs
from netbox_custom_objects.related_tabs.views.typed import (
    _make_badge,
    _make_typed_tab_view,
    _tab_label,
    _typed_queryset,
    reference_q,
    register_builtin_typed_tab,
)
from netbox_custom_objects.templatetags.custom_object_tab_tags import custom_objects_tab_link
from netbox_custom_objects.tests.base import CustomObjectsTestCase, TransactionCleanupMixin


class ReferenceQTests(TestCase):
    """
    ``reference_q()`` builds the correct filter per field kind, and — critically —
    returns an EMPTY Q (which callers must treat as "skip", never as match-all) for
    an unsupported field type or an unresolvable polymorphic through model. A
    regression here would leak every custom object of a type onto every host page.
    """

    def test_object_non_polymorphic(self):
        self.assertEqual(
            reference_q(1, 42, 'site', CustomFieldTypeChoices.TYPE_OBJECT, False, None),
            Q(site_id=42),
        )

    def test_object_polymorphic(self):
        self.assertEqual(
            reference_q(7, 42, 'thing', CustomFieldTypeChoices.TYPE_OBJECT, True, None),
            Q(thing_content_type_id=7, thing_object_id=42),
        )

    def test_multiobject_non_polymorphic(self):
        self.assertEqual(
            reference_q(1, 42, 'sites', CustomFieldTypeChoices.TYPE_MULTIOBJECT, False, None),
            Q(sites=42),
        )

    def test_unsupported_field_type_returns_empty_q(self):
        q = reference_q(1, 42, 'x', CustomFieldTypeChoices.TYPE_TEXT, False, None)
        self.assertFalse(q.children)  # empty Q == "skip", NOT match-all

    def test_unresolvable_through_returns_empty_q(self):
        # Polymorphic MULTIOBJECT whose through model isn't in the app registry.
        with self.assertLogs('netbox_custom_objects.related_tabs', level='ERROR'):
            q = reference_q(1, 42, 'x', CustomFieldTypeChoices.TYPE_MULTIOBJECT, True, 'Through_does_not_exist')
        self.assertFalse(q.children)


class TabLabelTests(TestCase):
    """``_tab_label()`` prefers the plural display name and never returns empty."""

    def test_prefers_verbose_name_plural(self):
        cot = SimpleNamespace(
            verbose_name_plural='Management Accesses', verbose_name='Management Access', name='management_access'
        )
        self.assertEqual(_tab_label(cot), 'Management Accesses')

    def test_falls_back_to_singular_then_name(self):
        self.assertEqual(
            _tab_label(SimpleNamespace(verbose_name_plural='', verbose_name='Widget', name='widget')), 'Widget'
        )
        self.assertEqual(
            _tab_label(SimpleNamespace(verbose_name_plural='', verbose_name='', name='widget')), 'widget'
        )


class RegisterTypedTabTests(TransactionCleanupMixin, CustomObjectsTestCase, TransactionTestCase):
    """
    ``register_builtin_typed_tab()`` adds one ``custom_objects_<slug>`` view per
    (host model, type) pair to NetBox's process-global view registry, idempotently —
    a second type referencing the same model gets its OWN tab.
    """

    def setUp(self):
        self.cot = self._site_cot('reg_cot', 'reg-cot')

    def tearDown(self):
        # Don't pollute the process-global registry for other tests.
        entries = registry['views'].get('dcim', {}).get('site', [])
        registry['views']['dcim']['site'] = [e for e in entries if not e['name'].startswith('custom_objects_')]
        super().tearDown()

    def _site_tab_names(self):
        return [e['name'] for e in registry['views'].get('dcim', {}).get('site', [])]

    def _site_cot(self, name, slug):
        cot = self.create_custom_object_type(name=name, slug=slug)
        self.create_custom_object_type_field(cot, name='name', label='Name', type='text', primary=True)
        self.create_custom_object_type_field(
            cot,
            name='site',
            label='Site',
            type=CustomFieldTypeChoices.TYPE_OBJECT,
            is_polymorphic=False,
            related_object_type=ObjectType.objects.get_for_model(Site),
        )
        return cot

    def test_registers_tab_view(self):
        register_builtin_typed_tab(Site, self.cot)
        self.assertIn('custom_objects_reg-cot', self._site_tab_names())

    def test_registration_is_idempotent(self):
        self.assertTrue(register_builtin_typed_tab(Site, self.cot))
        self.assertFalse(register_builtin_typed_tab(Site, self.cot))
        self.assertEqual(self._site_tab_names().count('custom_objects_reg-cot'), 1)

    def test_second_type_gets_its_own_tab(self):
        register_builtin_typed_tab(Site, self.cot)
        other = self._site_cot('reg_cot2', 'reg-cot2')
        register_builtin_typed_tab(Site, other)
        names = self._site_tab_names()
        self.assertIn('custom_objects_reg-cot', names)
        self.assertIn('custom_objects_reg-cot2', names)


class TypedQuerysetTests(TransactionCleanupMixin, CustomObjectsTestCase, TransactionTestCase):
    """
    ``_typed_queryset()`` is the SQL filter behind the tab's rows and badge:
    None when the type has no referencing fields for the host's content type,
    OR-merged across multiple referencing fields, and correct through the
    polymorphic MULTIOBJECT through table.
    """

    def _object_field_cot(self, name, slug):
        cot = self.create_custom_object_type(name=name, slug=slug)
        self.create_custom_object_type_field(cot, name='name', label='Name', type='text', primary=True)
        self.create_custom_object_type_field(
            cot,
            name='site',
            label='Site',
            type=CustomFieldTypeChoices.TYPE_OBJECT,
            is_polymorphic=False,
            related_object_type=ObjectType.objects.get_for_model(Site),
        )
        return cot

    def test_no_referencing_fields_returns_none(self):
        site = Site.objects.create(name='No Fields Site', slug='no-fields-site')
        cot = self.create_custom_object_type(name='empty_cot', slug='empty-cot')
        self.assertIsNone(_typed_queryset(cot, site))

    def test_row_referencing_via_either_field_is_matched_once(self):
        # Two fields of the SAME type pointing at Site (one OBJECT, one
        # MULTIOBJECT): a row matching via either — or both — appears exactly
        # once (the OR is a single SQL WHERE, not a union).
        site = Site.objects.create(name='OR Site', slug='or-site')
        cot = self.create_custom_object_type(name='or_cot', slug='or-cot')
        self.create_custom_object_type_field(cot, name='name', label='Name', type='text', primary=True)
        self.create_custom_object_type_field(
            cot, name='site', label='Site',
            type=CustomFieldTypeChoices.TYPE_OBJECT, is_polymorphic=False,
            related_object_type=ObjectType.objects.get_for_model(Site),
        )
        self.create_custom_object_type_field(
            cot, name='sites', label='Sites',
            type=CustomFieldTypeChoices.TYPE_MULTIOBJECT, is_polymorphic=False,
            related_object_type=ObjectType.objects.get_for_model(Site),
        )
        model = cot.get_model()
        row = model.objects.create(name='via-object', site=site)
        row.sites.set([site])
        model.objects.create(name='unrelated')  # references neither field

        qs = _typed_queryset(cot, site)
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.get().name, 'via-object')

        # A different site matches nothing (guards against a match-all filter).
        other = Site.objects.create(name='OR Other', slug='or-other')
        qs_other = _typed_queryset(cot, other)
        self.assertTrue(qs_other is None or qs_other.count() == 0)

    def test_polymorphic_multiobject_through_is_resolved(self):
        # Exercises reference_q()'s polymorphic-MULTIOBJECT subquery branch end to
        # end: a wrong through filter key would silently drop the row (count 0).
        from django.apps import apps as django_apps
        from django.contrib.contenttypes.models import ContentType

        site = Site.objects.create(name='Poly MO Site', slug='poly-mo-site')
        cot = self.create_custom_object_type(name='poly_mo', slug='poly-mo')
        self.create_custom_object_type_field(cot, name='name', label='Name', type='text', primary=True)
        field = self.create_polymorphic_field(
            cot,
            [ObjectType.objects.get_for_model(Site)],
            name='targets',
            type=CustomFieldTypeChoices.TYPE_MULTIOBJECT,
        )
        obj = cot.get_model().objects.create(name='mo-1')

        # Link the site through the field's through table, exactly as reference_q reads it.
        through = django_apps.get_model(APP_LABEL, field.through_model_name)
        through.objects.create(
            source_id=obj.pk,
            content_type_id=ContentType.objects.get_for_model(Site).id,
            object_id=site.pk,
        )

        self.assertEqual(_typed_queryset(cot, site).count(), 1)
        other = Site.objects.create(name='Poly MO Other', slug='poly-mo-other')
        # The type HAS a referencing field for Site, so the queryset is not
        # None — it just matches nothing (None is reserved for "no fields").
        qs_other = _typed_queryset(cot, other)
        self.assertIsNotNone(qs_other)
        self.assertEqual(qs_other.count(), 0)


class BadgeTests(TransactionCleanupMixin, CustomObjectsTestCase, TransactionTestCase):
    """
    The per-type badge is both the tab badge and the display gate. It must
    count only its OWN type's rows, return None for an unreferenced host
    (hide_if_empty), and restrict per user.
    """

    def _object_field_cot(self, name, slug):
        cot = self.create_custom_object_type(name=name, slug=slug)
        self.create_custom_object_type_field(cot, name='name', label='Name', type='text', primary=True)
        self.create_custom_object_type_field(
            cot,
            name='site',
            label='Site',
            type=CustomFieldTypeChoices.TYPE_OBJECT,
            is_polymorphic=False,
            related_object_type=ObjectType.objects.get_for_model(Site),
        )
        return cot

    def test_counts_only_its_own_type(self):
        site = Site.objects.create(name='Per Type Site', slug='per-type-site')
        cot_a = self._object_field_cot('badge_a', 'badge-a')
        cot_b = self._object_field_cot('badge_b', 'badge-b')
        cot_a.get_model().objects.create(name='a-1', site=site)
        cot_b.get_model().objects.create(name='b-1', site=site)

        self.assertEqual(_make_badge(cot_a)(site), 1)
        self.assertEqual(_make_badge(cot_b)(site), 1)

        # A different, unreferenced site gates to None.
        other = Site.objects.create(name='Per Type Other', slug='per-type-other')
        self.assertIsNone(_make_badge(cot_a)(other))

    def test_rows_and_badge_are_restricted_per_user(self):
        from django.contrib.auth import get_user_model

        site = Site.objects.create(name='Perm Site', slug='perm-site')
        cot = self._object_field_cot('perm_cot', 'perm-cot')
        cot.get_model().objects.create(name='co-1', site=site)
        limited = get_user_model().objects.create_user(username='limited-rows-2', password='x')

        badge = _make_badge(cot)

        # With no request context the badge falls back to an unrestricted count...
        self.assertEqual(badge(site), 1)

        # ...and with the limited user on the request, it restricts to the
        # same (empty) result, returning None so hide_if_empty hides the tab.
        req = RequestFactory().get('/')
        req.user = limited
        token = current_request.set(req)
        try:
            self.assertIsNone(badge(site))
        finally:
            current_request.reset(token)


class TypedViewChildrenTests(TransactionCleanupMixin, CustomObjectsTestCase, TransactionTestCase):
    """The registered tab view's get_children() returns the type's rows for the
    host, restricted per user."""

    def test_get_children_returns_rows_and_restricts(self):
        from django.contrib.auth import get_user_model

        site = Site.objects.create(name='Children Site', slug='children-site')
        cot = self.create_custom_object_type(name='children_cot', slug='children-cot')
        self.create_custom_object_type_field(cot, name='name', label='Name', type='text', primary=True)
        self.create_custom_object_type_field(
            cot,
            name='site',
            label='Site',
            type=CustomFieldTypeChoices.TYPE_OBJECT,
            is_polymorphic=False,
            related_object_type=ObjectType.objects.get_for_model(Site),
        )
        cot.get_model().objects.create(name='co-1', site=site)

        view_cls = _make_typed_tab_view(Site, cot)
        view = view_cls()

        # The full get() flow builds the table via get_table — exercise it
        # directly (a regression here 500s the tab while the page renders).
        request_superuser = RequestFactory().get('/')
        request_superuser.user = get_user_model().objects.filter(is_superuser=True).first()
        table = view.get_table(cot.get_model().objects.all(), request_superuser)
        self.assertIn('site', [c.name for c in table.columns])

        # A superuser sees the referencing row...
        request = RequestFactory().get('/')
        request.user = get_user_model().objects.filter(is_superuser=True).first()
        self.assertEqual(view.get_children(request, site).count(), 1)

        # ...a user with no object permissions sees none.
        limited_req = RequestFactory().get('/')
        limited_req.user = get_user_model().objects.create_user(username='children-viewer', password='x')
        self.assertEqual(view.get_children(limited_req, site).count(), 0)


class CoTabLinkTests(TransactionCleanupMixin, CustomObjectsTestCase, TransactionTestCase):
    """
    The ``custom_objects_tab_link`` tag renders one live link per referencing
    type on custom-object host pages (CO→CO), skipping zero-badge types —
    the CO→CO half of the no-restart guarantee.
    """

    def test_one_link_per_referencing_type(self):
        from django.contrib.auth import get_user_model

        # Host type A; target type B has an OBJECT field pointing at A.
        host_cot = self.create_custom_object_type(name='link_host', slug='link-host')
        self.create_custom_object_type_field(host_cot, name='name', label='Name', type='text', primary=True)
        target_cot = self.create_custom_object_type(name='link_target', slug='link-target')
        self.create_custom_object_type_field(target_cot, name='name', label='Name', type='text', primary=True)
        self.create_custom_object_type_field(
            target_cot,
            name='host',
            label='Host',
            type=CustomFieldTypeChoices.TYPE_OBJECT,
            is_polymorphic=False,
            related_object_type=host_cot.object_type,
        )

        host_row = host_cot.get_model().objects.create(name='host-1')
        target_cot.get_model().objects.create(name='target-1', host=host_row)

        admin = get_user_model().objects.filter(is_superuser=True).first()
        request = RequestFactory().get('/')
        request.user = admin

        result = custom_objects_tab_link({'request': request}, host_row)
        self.assertEqual(len(result['tabs']), 1)
        tab = result['tabs'][0]
        self.assertEqual(tab['badge'], 1)
        self.assertEqual(tab['label'], _tab_label(target_cot))
        self.assertFalse(tab['is_active'])

        # A host row nothing references renders no links (hide_if_empty).
        host_row2 = host_cot.get_model().objects.create(name='host-2')
        self.assertEqual(custom_objects_tab_link({'request': request}, host_row2)['tabs'], [])


class RegisterTabsSmokeTests(TestCase):
    """
    register_tabs() runs over live DB state without raising (ready() already
    called it during setup; calling it again must be safe).
    """

    def test_register_tabs_does_not_raise(self):
        names_before = self._typed_tab_names()
        register_tabs()
        # Idempotent: a second pass adds no new tabs beyond what the first
        # (ready()) pass registered for the same DB state.
        self.assertEqual(self._typed_tab_names(), names_before)

    @staticmethod
    def _typed_tab_names():
        return [
            entry['name']
            for app_entries in registry['views'].values()
            for model_entries in app_entries.values()
            for entry in model_entries
            if entry['name'].startswith('custom_objects_')
        ]
