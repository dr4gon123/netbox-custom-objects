"""
Related-object tabs for netbox-custom-objects — one tab PER referencing
CustomObjectType, rendering that type's real columns.

Every model referenced by a Custom Object Type Object/Multi-object field
gets one tab per referencing type on its detail page: labeled with the
type's plural name, badged with the rows the viewer may see, hidden when
empty, and rendering the same dynamic table as the type's list view
(field columns, per-row actions, column preferences) — filtered in SQL
to the rows referencing the host object.  This replaces the former
single combined "Custom Objects" tab, which listed references
generically and could not show the referenced rows' content.

Registration happens once, at startup, by ``registry.register_tabs()``
(called from ``CustomObjectsPluginConfig.ready()``). What it registers
depends on the host kind:

* **Built-in NetBox models** (Device, Site, …) — the plugin does NOT own
  their templates, so tabs render via NetBox's registry-driven tab
  machinery with per-(model, type) routes baked by ``get_model_urls()``
  at URLconf freeze (the root URLconf is built once, after ``ready()``,
  on the first request).  Pairs are enumerated from the fields that
  exist at startup; a pair created later (a new type, or a type's first
  field pointing at a model) needs a web restart to get its tab.  Rows,
  counts, and columns are live regardless — they resolve per request,
  and ``hide_if_empty`` hides a tab whose restricted count is zero.

* **Custom-object host pages** (a COT field that targets another COT —
  CO→CO) — the plugin owns ``customobject.html``, so the per-type nav
  links are rendered live by the ``custom_objects_tab_link`` template
  tag (computed from the DB per render), and their URL is a single
  generic route injected once at startup (``_inject_co_urls``) that
  reverses for *any* host/target slug pair, including COTs created
  later.  CO→CO tabs are therefore live, with no restart.

This keeps the feature free of any runtime URL-resolver mutation or
cross-worker coordination machinery (no middleware, no signals, no
shared cache backend): the view registry and URLconf are populated once
at startup, and everything user-visible is driven by per-request DB
reads.
"""
