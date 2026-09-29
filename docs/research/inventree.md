# InvenTree research for the Slack connector

Research date: 2026-09-28. Sources are the public InvenTree docs, the `inventree/InvenTree` and
`inventree/inventree-python` GitHub repos, PyPI and Docker Hub. Nothing here was tested against a
running server. Source code was read through a fetch tool that can truncate large files, so every
claim is tagged where it matters:

- **[src 1.0.0] / [src stable] / [src master]** - read from source at that git ref (`stable` = 1.5.6).
- **[docs]** - stated in official docs.
- **[inferred]** - my reading of the code, not stated anywhere and not executed.
- **[unverified]** - could not confirm from a primary source.

## Key takeaways for the connector

1. Stock adjustments are `POST /api/stock/{transfer,add,remove,count,merge,assign,return,change_status}/`
   with body `{"items": [{"pk", "quantity"}], "notes", ...}`. Each request runs inside one
   `transaction.atomic()` block, so a multi-item request is all-or-nothing. Success is HTTP 201.
2. A partial transfer **splits** the stock item: a new StockItem (new pk, `parent` = source) is created
   at the destination and the source keeps its pk with reduced quantity. The new pk is not known up front.
3. **The server does not reject over-quantity requests** [src 1.0.0, inferred for 1.5.x]. Transfer of
   more than the item holds moves the whole item; remove of more than it holds clamps to zero. The
   connector must pre-check quantity itself and re-read afterwards.
4. Removing to zero can **delete** the stock item (`delete_on_deplete`, default on). Read-back by pk
   then returns 404. **Measured 2026-09-29 on 1.4.3 and 1.5.6: the tracking history is NOT deleted**
   (this note first said it was), and `track/?item=<deleted pk>` answers 400.
5. Serialized stock items always have quantity 1. add/remove on them is a silent no-op that still
   returns 201; they can only be moved whole.
6. There is **no native idempotency support**. Best available mechanism: put a unique key in the
   adjustment `notes` (stored on the tracking entry, max 512 chars) and look it up with
   `GET /api/stock/track/?search=<key>` before retrying.
7. `/api/stock/track/` is read-only and good for read-back verification: it records type code, deltas,
   user, date and notes per stock item.
8. Auth is `Authorization: Token <value>`. Tokens carry the full permissions of their user (no per-token
   scope field), expire after 365 days by default, and change format in the upcoming 1.6.0.
9. Current stable is **1.5.6 (2026-09-26), API version 530**. Minor releases land roughly every two
   months and the API changes additively but frequently; gate features on `apiVersion`, not on semver.
10. Call REST directly with httpx rather than depending on `inventree` (sync-only, no retries, stock
    helper methods discard the response). Run the connector as a standalone service, not a plugin.

---

## 1. Stock adjustment endpoints

All are `POST` only, all extend DRF `CreateAPI`, registered under `/api/stock/` [src master `stock/api.py`].

| Path | Serializer | Extra top-level fields |
|---|---|---|
| `/api/stock/transfer/` | `StockTransferSerializer` | `location` (required, destination pk) |
| `/api/stock/add/` | `StockAddSerializer` | - |
| `/api/stock/remove/` | `StockRemoveSerializer` | - |
| `/api/stock/count/` | `StockCountSerializer` | `location` (optional, API >= 497 / 1.4.0+) |
| `/api/stock/return/` | `StockReturnSerializer` | `location` (required), `merge` (bool, default false) |
| `/api/stock/merge/` | `StockMergeSerializer` | `location` (required), `allow_mismatched_suppliers`, `allow_mismatched_status` |
| `/api/stock/assign/` | `StockAssignmentSerializer` | `customer` (required, company pk) |
| `/api/stock/change_status/` | `StockChangeStatusSerializer` | `status`, `note` (singular); `items` is a plain list of pks |

### Item schema (transfer, add, remove, count, return) [src stable]

| Field | Type | Required | Notes |
|---|---|---|---|
| `pk` | int | yes | StockItem primary key |
| `quantity` | decimal, max 15 digits, 5 decimal places, min 0 | yes | must be > 0 for transfer and return |
| `batch` | string, max 100 | no | overrides batch code |
| `status` | int status code | no | overrides stock status |
| `packaging` | string, max 50 | no | overrides packaging |
| `merge` | bool, default false | no | API >= 512 (1.5.0+); only acted on by transfer |

`merge` and `assign` use `{"item": <pk>}` per entry, not `{"pk", "quantity"}`. Merge needs at least two
items; the first is the base item that survives.

### Example: move 2 units to Sam's truck

```http
POST /api/stock/transfer/
Authorization: Token <token>
Content-Type: application/json

{
  "items": [{"pk": 1234, "quantity": 2}],
  "location": 57,
  "notes": "[sic:k7m2q9x4t1b8w3n6p5r0zd:a01] slack C0FIELD/1727555555.000100 by U0SAM0001"
}
```

Expected: `201 Created`. The response body is whatever DRF serializes back from the input
serializer; **it does not include the pk of any newly created stock item** [inferred, unverified].
Validation failures return `400` with a field-keyed error object [unverified shape]. Known messages
from source: `"A list of stock items must be provided"`, `"Quantity must be greater than zero"`,
`"Stock item is not in stock"`, `"Structural locations cannot be assigned stock items"`.

### Atomicity

Every `save()` wraps its item loop in `transaction.atomic()` [src 1.0.0 and src stable]. From 1.5.0
(PR 12488) the block also batches events, tracking entries and background tasks, and items are sorted
by pk before processing to avoid deadlocks between concurrent requests. That PR was a performance
refactor; no request schema change was observed.

### Partial quantity, over-quantity and zero [src 1.0.0 `stock/models.py`]

`StockItem.move()`:

- `quantity < item.quantity`: calls `splitStock()`. A copy of the item is created with `pk=None`,
  `parent=<source>`, the requested quantity and the destination location. The source is reduced via
  `take_stock()`. Returns without moving the source.
- `quantity >= item.quantity`: the **whole item** is relocated. There is no error when the request
  exceeds what the item holds.
- Destination equal to current location: recorded as `STOCK_UPDATE` rather than `STOCK_MOVE`.

`take_stock()` / `updateQuantity()`:

- New quantity is clamped with `max(quantity, 0)`; removing more than exists yields zero, no error.
- At zero, if `delete_on_deplete` is set and the item can be deleted, the item is deleted and no
  tracking entry is written. The global default "Delete Depleted Stock" is `True` [docs].

PR 12488 (1.5.0) did not modify `move()`, `splitStock()` or `updateQuantity()`, and PR 12022 only
added tracking-suppression flags to `splitStock()`, so this behaviour very likely holds in 1.5.x, but
I could not read the 1.5.6 `models.py` directly (file too large for the fetch tool).

### Merge on transfer (API >= 512)

With `"merge": true` on an item, the server looks for an existing stock item of the same part at the
destination (preferring a matching batch) that passes `can_merge()`, and merges into it instead of
leaving a separate item. If no target is found it falls back to a normal move. Default for the UI is
the global setting "Merge stock on transfer" (`False`). Without merge, repeated partial transfers to a
truck produce many small stock items for the same part.

### Permissions

Default permission classes are `IsAuthenticated`, `ModelPermission`, `RolePermission`,
`InvenTreeTokenMatchesOASRequirements` [src stable `settings.py`]. `RolePermission` maps
`POST -> add`, `GET -> view`, `PUT/PATCH -> change`, `DELETE -> delete` and resolves the role from the
view's queryset model. The adjustment views declare `queryset = StockItem...` and no `role_required`,
and `stock_stockitem` belongs to the `stock` ruleset. So the token's user needs the **Stock Item role
with "add"** (plus "view" on Stock Item, Stock Location and Part for lookups) [inferred]. Confirm with
a restricted test user. If "Stock Ownership Control" is enabled, owner checks may also apply [unverified].

Sources:
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/stock/api.py
- https://github.com/inventree/InvenTree/blob/stable/src/backend/InvenTree/stock/serializers.py
- https://github.com/inventree/InvenTree/blob/1.0.0/src/backend/InvenTree/stock/serializers.py
- https://github.com/inventree/InvenTree/blob/1.0.0/src/backend/InvenTree/stock/models.py
- https://github.com/inventree/InvenTree/pull/12488 , https://github.com/inventree/InvenTree/pull/12022
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/InvenTree/permissions.py
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/users/ruleset.py
- https://docs.inventree.org/en/stable/stock/adjust/ , https://docs.inventree.org/en/stable/settings/global/

## 2. Stock items vs parts

"A Stock Item is an actual instance of a Part item. It represents a physical quantity of the Part in
a specific location" [docs]. One part can have many stock items, including several in the same
location (different batches, or leftovers from splits).

### Listing stock: `GET /api/stock/`

Filters [src master `StockFilter`; assumed same on stable]:

| Param | Behaviour |
|---|---|
| `part` | part pk; add `include_variants` to include variants of a template |
| `location` | location pk, or `null` for unlocated stock |
| `cascade` | **defaults to true**: includes sub-locations. Pass `cascade=false` for that location only |
| `in_stock` | true = item counts as in stock |
| `available` | in stock and `quantity > allocated` |
| `serial` | exact match; also `serial_gte`, `serial_lte` |
| `serialized`, `batch`, `batch_regex`, `has_batch` | serial / batch filters |
| `status` | integer status code (also matches custom status keys) |
| `IPN`, `IPN_contains`, `name_contains`, `category`, `active` | filter by attributes of the part |
| `external`, `allocated`, `depleted`, `tracked`, `expired` | booleans |

`search` covers serial, batch, location name, part name, part IPN, part description, SKU, MPN, tags.
Pagination is limit/offset (`limit`, `offset`).

```http
GET /api/stock/?part=88&location=57&cascade=false&in_stock=true&limit=50
```

Useful response fields: `pk`, `part`, `quantity`, `serial`, `batch`, `location`, `status`,
`status_text`, `in_stock`, `allocated`, `parent`, `delete_on_deplete`, `updated`, `tags`. Optional
expansions include `part_detail` and `location_detail`. `location` is read-only on the stock item
serializer, so moving stock must go through the transfer endpoint [src stable].

Stock status codes: 10 OK, 50 Attention, 55 Damaged, 60 Destroyed, 65 Rejected, 70 Lost,
75 Quarantined, 85 Returned.

### Serialized / trackable parts

- `trackable` parts "can be assigned batch numbers or serial numbers which uniquely identify a
  particular stock item" [docs].
- Model validation: "Quantity must be 1 for item with a serial number" [src master].
- Serials are unique per part (including variants) by default; optionally globally unique [docs].
- `splitStock()`, `add_stock()`, `take_stock()` and `updateQuantity()` all return early for serialized
  items [src 1.0.0]. Consequence: "2 units" of a serialized part means two separate stock items, each
  sent as `{"pk": n, "quantity": 1}`. The connector must choose which serials to move.
- Serialized items cannot be merged.

Sources:
- https://docs.inventree.org/en/stable/stock/ , https://docs.inventree.org/en/stable/stock/traceability/
- https://docs.inventree.org/en/stable/part/
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/stock/api.py
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/stock/status_codes.py

## 3. Stock tracking / history and idempotency

### Endpoint

`GET /api/stock/track/` and `GET /api/stock/track/<pk>/`. Read-only: entries are created by model
code only.

| Field | Meaning |
|---|---|
| `pk`, `item` | entry id, stock item pk |
| `part` | part pk (API >= 450, 1.2.0+) |
| `tracking_type`, `label` | integer history code and its text |
| `date` | set automatically on creation |
| `user` | user who made the change |
| `notes` | the `notes` string from the adjustment request, max 512 chars on the model |
| `deltas` | JSON describing the change |

Filters: `item`, `user` (all versions); `part`, `include_variants`, `min_date`, `max_date`
(API >= 450). `search` matches `notes`. Ordering by `date`, default newest first. Optional
`item_detail=true`, `user_detail=true`. The list view also expands ids inside `deltas` into
`location_detail`, `stockitem_detail` and similar.

### What a transfer writes [src 1.0.0]

| Action | Entry on | Code | Deltas |
|---|---|---|---|
| Full transfer | moved item | 20 `STOCK_MOVE` | `location`, plus `status`/`batch`/`packaging` if changed |
| Same-location transfer | item | 25 `STOCK_UPDATE` | changed fields only |
| Partial transfer | new item | 40 `SPLIT_FROM_PARENT` | `stockitem` = source pk, plus quantity/location |
| Partial transfer | source item | 42 `SPLIT_CHILD_ITEM` | `removed`, `quantity` (remaining), `location`, `stockitem` = new pk |
| Add | item | 11 `STOCK_ADD` | `added`, `quantity` |
| Remove | item | 12 `STOCK_REMOVE` | `removed`, `quantity` |
| Count | item | 10 `STOCK_COUNT` | `quantity` |
| Merge | surviving item | 45 `MERGED_STOCK_ITEMS` | `quantity`, `added` (API >= 512), `location` |

Note that a partial transfer does **not** emit code 20; filter on 40/42.

### Read-back verification

Workable. After a write: `GET /api/stock/track/?item=<source pk>&limit=5`, match on the notes key and
code, and for partial transfers take the new item pk from `deltas.stockitem` of the code-42 entry.
Then `GET /api/stock/<pk>/` for both items to confirm quantity and location. Caveat: a deleted
(depleted) item takes its tracking rows with it (`on_delete=CASCADE`). **Wrong, measured on 1.4.3 and
1.5.6: the rows survive, and the `item` filter answers 400 for a deleted item.** Docs also mention a
setting to delete old tracking entries.

### Where to store an external reference

| Option | Verdict |
|---|---|
| Adjustment `notes` | Best. Lands on the tracking entry, searchable via `?search=`. Keep under 512 chars. |
| Metadata JSON, `/api/metadata/<model>/<pk>/` | Exists on most models, "for use by external plugins". PUT overwrites, PATCH merges. Per object, not per movement. Before API 436 the path was `<object url>/metadata/`. Tracking entries do not appear to have it [unverified]. |
| Tags | On StockItem and StockLocation; more models at API >= 500. Per object, not per movement. |
| Parameters | StockLocation has parameters at API >= 437 [unverified version number]. Not per movement. |
| StockItem `notes` field | Avoid: changelog v537 (master, 1.6.0) removes direct `notes` fields in favour of a generic Note model. |

### Native idempotency

None found. No idempotency-key header or duplicate detection appears in the docs, API changelog or
the adjustment serializers. Repeating a POST repeats the movement. The connector needs its own
durable outbox keyed by Slack message id, plus the notes lookup above for crash recovery.

Sources:
- https://docs.inventree.org/en/stable/stock/tracking/ , https://docs.inventree.org/en/stable/plugins/metadata/
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/stock/api.py
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/stock/serializers.py
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/stock/status_codes.py
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/InvenTree/api_version.py

## 4. Locations

`/api/stock/location/` (list, create), `/api/stock/location/<pk>/`, `/api/stock/location/tree/`,
`/api/stock/location-type/` [src master].

Fields: `pk`, `name`, `description`, `parent`, `level`, `pathstring`, `path` (opt-in via
`path_detail=true`), `items`, `sublocations`, `owner`, `icon`, `custom_icon`, `structural`,
`external`, `location_type`, `location_type_detail`, `tags`, `parameters`, `barcode_hash`.
Read-only: `pathstring`, `level`, `icon`, `barcode_hash`.

Filters: `name`, `structural`, `external`, `location_type`, `has_location_type`, `parent`, `cascade`,
`depth`, `top_level`, `tag_name`. `search` covers name, description, pathstring and tags.

- **pathstring**: server-generated full path, e.g. `Vehicles/Sam's truck`. Searchable, so it is the
  best field for resolving "my truck" style references.
- **structural**: locations "not directly associated with stock items, but rather serve as a means of
  organizing the stock location hierarchy". Transfer, count and return reject them.
- **external**: indicates "items in there might not be available for immediate usage". It "does not
  get inherited to sublocations". Parts expose an `external_stock` total.
- **location types**: name, description, icon; used to standardize icons across similar locations.

### Modelling a vehicle (suggestion)

```
Vehicles                 structural=true
  Sam's truck            location_type=Vehicle, owner=<Sam or group>
    Bin A                optional sub-location
```

Create a `Vehicle` location type and filter with `?location_type=<pk>&structural=false`. Whether to
set `external=true` on trucks is a business decision for the admin (it changes what counts as
immediately available); set it on each truck individually since it is not inherited. Mapping a Slack
user to a truck is not something InvenTree provides; `owner` or location metadata could hold it.

Sources:
- https://docs.inventree.org/en/stable/stock/
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/stock/serializers.py
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/stock/api.py

## 5. Parts search

`GET /api/part/` [src master `part/api.py`; assumed same on stable].

- `search=<text>` matches `name`, `description`, `IPN`, `revision`, `keywords`, `category__name`,
  manufacturer part `MPN`, supplier part `SKU`, and tags.
- `IPN` (case-insensitive exact), `IPN_regex`, `name_regex`. No plain exact `name` filter was listed.
- `category` with `cascade`; `variant_of`, `ancestor`, `revision_of`.
- Booleans: `active`, `trackable`, `is_template`, `is_variant`, `is_revision`, `virtual`, `component`,
  `assembly`, `purchaseable`, `salable`, `locked`, `has_stock`, `low_stock`, `has_units`, `has_ipn`.

```http
GET /api/part/?search=Disc%2040&active=true&virtual=false&limit=10
```

Fields worth using for matching: `name`, `IPN`, `full_name` (server-composed display name),
`description`, `keywords`, `revision`, `units`, `trackable`, `is_template`, `variant_of`, `active`,
`virtual`, `category_name`, `in_stock`, `total_in_stock`, `tags`.

Matching guidance [inferred]: a template part (`is_template=true`) is an umbrella for variants; stock
normally sits on the variants, so resolve to a concrete part or ask the user. Exclude
`active=false` and `virtual=true`. The server search is substring-style, not fuzzy; do fuzzy ranking
client-side over the returned candidates.

### Units and pack size

- Part `units` is a physical unit handled by the `pint` library; blank means dimensionless. Built-in
  dimensionless units include piece, each, dozen, hundred, thousand [docs].
- Stock quantities are always in **part units**.
- Pack size belongs to the **supplier part** (default 1). "If a supplier part with a pack size of 5 is
  bought in a quantity of 4, 20 parts will be added to stock when the parts are received" [docs].
- `use_pack_size` exists on stock item creation. It is **not** in the adjustment item schema, so
  "2 boxes" must be converted to part units by the connector before calling add/transfer.

Sources:
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/part/api.py
- https://github.com/inventree/InvenTree/blob/stable/src/backend/InvenTree/part/serializers.py
- https://docs.inventree.org/en/stable/part/ , https://docs.inventree.org/en/stable/concepts/units/
- https://docs.inventree.org/en/stable/purchasing/supplier/

## 6. Auth

- Header: `Authorization: Token <TOKEN-VALUE>` [docs]. Middleware source also lists `bearer` as an
  accepted prefix, but I did not confirm the DRF authentication class accepts it; use `Token`.
- Authentication classes in order: API token, HTTP basic, session, OAuth2 [src stable].
- Create a token: UI (user settings, security section) or
  `GET /api/user/me/token/?name=<name>` with basic auth, returning `{"token", "name", "expiry"}`.
  Before API 490 the path was `/api/user/token/`.
- Multiple named tokens per user are supported. **Requesting a token with a name that already exists
  revokes the old one and issues a new one** [src master, docs latest; not confirmed for 1.5.x]. Do
  not fetch a token on every start with a fixed name if other processes share it.
- Expiry: model default is today + 365 days [src stable and master]. Token management endpoints:
  `/api/user/tokens/` and `/api/user/tokens/<pk>/`.
- Format: 1.5.x tokens look like `inv-<key>-<yyyymmdd>`. From 1.6.0 a v2 format (`inv-2-` prefix)
  stores only an HMAC digest, and "Tokens are only displayed or provided once" [docs latest].
- Scoping: the `ApiToken` model has no scope field, so a token has all permissions of its user
  [inferred]. Scoped tokens exist only via OAuth2, which the stable docs call experimental.
- Recommended setup: a dedicated service user in a group granting Stock Item view/add/change,
  Stock Location view and Part view. Note that tracking entries will then name the service user,
  so the human Slack user should be written into `notes`.
- Roles: Admin, Part Category, Part, BOM, Stock Location, Stock Item, Build, Purchase Order,
  Sales Order, Return Order, Transfer Order; each with view/change/add/delete. Roles are assigned to
  groups. `GET /api/user/me/roles/` returns the caller's roles. Denied calls return 403.
- Throttling (1.5.0+): defaults `20/minute` anonymous and `60/second` per user, returns 429, disabled
  when `INVENTREE_DEBUG` is on, configurable via `INVENTREE_THROTTLE_ANON` / `INVENTREE_THROTTLE_USER`.

Sources:
- https://docs.inventree.org/en/stable/api/ , https://docs.inventree.org/en/latest/api/
- https://docs.inventree.org/en/stable/settings/permissions/ , https://docs.inventree.org/en/latest/start/config/
- https://github.com/inventree/InvenTree/blob/stable/src/backend/InvenTree/users/models.py
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/users/api.py
- https://github.com/inventree/InvenTree/blob/stable/src/backend/InvenTree/InvenTree/settings.py
- https://github.com/inventree/InvenTree/pull/11951

## 7. API versioning and supported versions

- `GET /api/` works without authentication and returns, among others: `server` ("InvenTree"),
  `version`, `instance`, `apiVersion`, `worker_running`, `plugins_enabled`, `active_plugins`,
  `debug_mode`, `docker_mode`. Staff callers get extra fields [src master].
- Every response carries headers `X-InvenTree-Version` and `X-InvenTree-API` [src stable].
- `apiVersion` is a single integer that increments with each API change; the changelog lives in
  `api_version.py`. There is no URL versioning (`/api/v1/`), so one path serves whatever the server runs.
- Docs state the project follows semantic versioning; `stable` branch = latest tagged release,
  `master` = development.

| Release | Date | API version |
|---|---|---|
| 0.17.0 / 0.17.14 | 2024-12-17 / 2025-06-21 | 294 (at 0.17.14) |
| 1.0.0 | 2025-09-14 | 391 |
| 1.1.0 | 2025-11-02 | 421 |
| 1.2.0 | 2026-02-12 | 453 |
| 1.3.0 | 2026-04-11 | 477 |
| 1.4.0 / 1.4.4 | 2026-06-24 / 2026-08-10 | 511 (at 1.4.4) |
| 1.5.0 | 2026-08-11 | 530 (read from `stable`) |
| **1.5.6 (current stable)** | **2026-09-26** | **530** |
| master (`1.6.0 dev`) | unreleased | 550 |

Cadence: five minor releases in the 11 months after 1.0.0, so roughly every 7 to 14 weeks, with patch
releases every one to two weeks. The last patch of each minor was published within days of the next
minor (1.2.7 on 2026-04-07, 1.3.6 on 2026-06-18, 1.4.4 on 2026-08-10), which suggests only the newest
minor receives fixes [inferred; no written support policy found].

### Suggested support window

- Test matrix: `1.5.6` (pinned), `1.4.4`, and `stable` as a floating canary. Optionally `latest`
  as a non-blocking job to catch 1.6 changes early.
- Minimum: API 497 if count-to-location is used, API 512 if merge-on-transfer is used, otherwise
  API 450 (1.2.0) gives the richer tracking filters. Going below 1.0.0 is not worth it.
- Runtime: read `apiVersion` at startup, refuse to start below the minimum, and feature-gate on it.

### Changes touching stock adjustments

Version numbers below were read from the changelog via a summarizing tool and two passes disagreed by
one on some entries; the PR links are the reliable reference.

| API | Date | Change | First release |
|---|---|---|---|
| 133 | 2023-09-08 | optional `batch`/`status`/`packaging` on adjustment items (PR 5518) | 0.13 [unverified] |
| 288 or 289 | 2024-11-27 | status change during transfer; transfer allowed regardless of status (PR 8570) | 0.17.0 |
| 381 | 2025-08-06 | return endpoint refactored to match other adjustments (PR 10132) | 1.0.0 |
| 391 | 2025-09-06 | StockList `cascade`/`location`/`exclude_tree` filter refactor (PR 10279) | 1.0.0 |
| 436 | 2026-01-06 | per-model metadata endpoints replaced by `/api/metadata/<model>/` (PR 11035) | 1.2.0 |
| 450 | 2026-02-10 | `part` field and new filters on tracking (PR 11260) | 1.2.0 |
| 490 | 2026-05-19 | self endpoints moved under `/api/user/me/`, including token | 1.4.0 |
| 492 | 2026-05-22 | Transfer Order model and API (PR 11281) | 1.4.0 |
| 497 | 2026-05-27 | optional `location` on count (PR 12019) | 1.4.0 |
| 512 | 2026-06-20 | optional per-item `merge` on transfer (PR 12022) | 1.5.0 |
| n/a | 2026-07-28 | adjustment endpoints performance refactor (PR 12488) | 1.5.0 |
| n/a | 2026-08-10 | API throttling on by default (PR 11951) | 1.5.0 |
| 537 | 2026-08-31 | direct `notes` fields replaced by generic Note model (PR 11971) | 1.6.0 |
| 547 | 2026-09-12 | token management changes (PR 12850) | 1.6.0 |

None of these changed the core `items: [{pk, quantity}]` contract. Transfer Orders (1.4.0+) are a
heavier, stateful alternative (pending, issued, on hold, complete, cancelled) for requested moves;
probably too formal for chat-driven adjustments but worth knowing about.

Sources:
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/InvenTree/api_version.py
- https://github.com/inventree/InvenTree/blob/master/src/backend/InvenTree/InvenTree/api.py
- https://github.com/inventree/InvenTree/blob/stable/src/backend/InvenTree/InvenTree/version.py
- https://github.com/inventree/InvenTree/blob/stable/src/backend/InvenTree/InvenTree/middleware.py
- https://docs.inventree.org/en/stable/releases/release_notes/
- https://github.com/inventree/InvenTree/releases , https://github.com/inventree/InvenTree/releases/tag/1.5.0
- https://docs.inventree.org/en/stable/stock/transfer_order/

## 8. Official Python client (`inventree`)

| Item | Finding |
|---|---|
| Version | 0.23.2, released 2026-05-13. Previous: 0.23.1 (2026-03-02), 0.23.0 (2026-02-15), 0.22.0 (2026-02-11) |
| Maintenance | Active, maintained by the core InvenTree developers. Last commit seen 2026-08-04 |
| Python | `>=3.9` on PyPI; CI runs on 3.12 only |
| Dependencies | `requests>=2.27.0`, `urllib3>=2.3.0`; optional `pip-system-certs` |
| License | MIT |
| HTTP | `requests`, synchronous only. No async API |
| Retries | None. Timeouts are logged and re-raised. Default timeout 10 s |
| Errors | `requests.exceptions.HTTPError` for status >= 300, with a detail dict (url, status, body) |
| Server check | On connect, `GET /api/`, requires `server == "inventree"` and `apiVersion >= 206` |
| Per-class gating | `MIN_API_VERSION` / `MAX_API_VERSION`; out of range raises `NotImplementedError` |
| Config | `INVENTREE_API_HOST`, `_USERNAME`, `_PASSWORD`, `_TOKEN`, `_TOKEN_NAME`, `_TIMEOUT` |

Stock methods on `inventree.stock.StockItem`:

- Class methods: `adjustStockItems(api, method, items, **kwargs)`, `countStockItems`, `addStockItems`,
  `removeStockItems`, `transferStockItems(api, items, location, **kwargs)`, `assignStockItems`.
  These return the POST response.
- Instance methods: `countStock(quantity)`, `addStock(quantity)`, `removeStock(quantity)`,
  `transferStock(location, quantity=None)`, `assignStock(customer)`, `installStock`, `uninstallStock`,
  `getTrackingEntries()`. The count/add/remove/transfer instance methods **do not return** the response.
- `adjustStockItems` only accepts `count`, `add`, `remove`, `transfer`, `assign`. No helper for
  `merge`, `return` or `change_status`.
- `getMetadata()` / `setMetadata(data, overwrite=False)` handle both metadata URL schemes.

Limitations: sync only, no retry or backoff, no 429 handling, no typed models (attribute access over
raw JSON), and when constructed with username/password it requests a token named
`inventree-python-client`, which on newer servers revokes any existing token of that name.

### Recommendation: call REST directly with httpx

- The connector uses about six endpoints; the wrapper adds little over a thin typed client.
- Slack handlers are usually async; the client would need thread offloading.
- The connector needs its own retry, timeout, 429 and idempotency logic around each call, plus access
  to the raw response and headers (`X-InvenTree-API`).
- Own the version gating rather than inheriting the client's `apiVersion >= 206` floor.
- Still useful: read the client's source as a reference, and use it in test fixtures or seed scripts.

Sources:
- https://pypi.org/project/inventree/ , https://github.com/inventree/inventree-python
- https://github.com/inventree/inventree-python/blob/master/inventree/api.py
- https://github.com/inventree/inventree-python/blob/master/inventree/base.py
- https://github.com/inventree/inventree-python/blob/master/inventree/stock.py
- https://docs.inventree.org/en/stable/api/python/ , https://docs.inventree.org/en/stable/api/python/examples/

## 9. Plugin system

Mixins listed in the docs: Action, APICall, App (extra database tables), Barcode, CurrencyExchange,
DataExport, Event, LabelPrinting, Locate, MachineDriver, Mail, Navigation, Notification, Report,
Schedule, Settings, UserInterface, Urls, Validation.

- **Install / distribute**: PyPI package with an `inventree_plugins` entry point; installed through
  the web UI (superuser only), a `plugins.txt` file plus `invoke plugins`, or a local plugin
  directory (`INVENTREE_PLUGIN_DIR`, "not recommended for production").
- **Enablement**: `INVENTREE_PLUGINS_ENABLED` must be set in server config (default `False`), each
  plugin must be activated, and URL, event, schedule, app, navigation and interface integration are
  separate global settings that all default to `False`. Plugins load at server start only. In
  containers "Check plugins on startup" must be on because the environment is ephemeral.
- **Runtime**: plugin code runs inside the InvenTree Django server and worker processes with full
  database access. Events are delivered by the background worker. Custom URLs are served under
  `/plugin/<slug>/`. Plugins can declare `MIN_VERSION` / `MAX_VERSION`.
- **Security**: docs warn third-party plugins are unsupported and used "at your own risk".
- Stock events available to `EventMixin` include `stockitem.moved`, `stockitem.split`,
  `stockitem.counted`, `stockitem.quantityupdated`, `stockitem.returnedtostock`,
  `stockitem.assignedtocustomer`.

### Built-in Slack plugin

`InvenTreeSlackNotifications` (slug `inventree-slack-notification`) is optional and outbound only. It
has one protected setting, `NOTIFICATION_SLACK_URL`, and `requests.post()`s a `text` + `blocks`
payload to that URL when InvenTree raises a notification [src stable]. It receives nothing from
Slack, parses nothing, and performs no stock actions. The docs page summary mentioned a "Slack API
key" while the source uses a webhook-style URL; treat the docs wording as unverified.

### Could the connector be a plugin?

Technically yes (UrlsMixin for the Slack events endpoint, SettingsMixin for secrets), but I would not:

| | Plugin | Standalone service |
|---|---|---|
| Deployment | Admin must enable plugins and URL integration, restart server | Separate container, needs only URL and token |
| Version coupling | Tied to server Python/Django and internal model APIs | Tied only to REST `apiVersion` |
| Blast radius | Runs with full DB access inside the inventory server | Limited to the service user's roles |
| Slack ingress | InvenTree itself must be reachable from Slack, or no Socket Mode | Socket Mode or its own endpoint |
| State (outbox, pending confirmations) | Needs AppMixin migrations in the InvenTree DB | Own store |
| Writes | Model calls bypass API validation unless done carefully | Uses the validated public API |
| Works with hosted / managed InvenTree | Often not allowed | Yes |

A small optional companion plugin could be added later for event push (`stockitem.moved` to Slack)
if polling proves insufficient.

Sources:
- https://docs.inventree.org/en/stable/plugins/ , https://docs.inventree.org/en/stable/plugins/install/
- https://docs.inventree.org/en/stable/plugins/develop/
- https://docs.inventree.org/en/stable/plugins/mixins/event/ , https://docs.inventree.org/en/stable/plugins/mixins/urls/
- https://docs.inventree.org/en/stable/plugins/builtin/slack_notification/
- https://github.com/inventree/InvenTree/blob/stable/src/backend/InvenTree/plugin/builtin/integration/core_notifications.py
- https://docs.inventree.org/en/stable/settings/global/ , https://docs.inventree.org/en/stable/start/config/

## 10. InvenTree in Docker for integration tests

- Image: `inventree/inventree` on Docker Hub. Tags: `stable` (latest release), `latest` (master
  development build), and exact versions such as `1.5.6`. Checked 2026-09-29: the 1.4 line ends at
  `1.4.3` on Docker Hub (there is no `1.4.4` image), and 1.4.3 is also API 511. Architectures amd64 and arm64.
  Compressed size about 500 to 520 MB.
- Production compose (`contrib/container/`: `docker-compose.yml`, `.env`, `Caddyfile`) runs five
  services: PostgreSQL 17, Redis, server (gunicorn), worker (django-q), Caddy proxy. Too heavy for CI.

### Minimal single-container setup with SQLite

This is what `inventree-python` uses for local tests (`test/docker-compose.yml`), adapted to a pinned tag:

```yaml
services:
  inventree:
    image: inventree/inventree:1.5.6
    ports: ["12345:8000"]
    environment:
      - INVENTREE_DEBUG=True
      - INVENTREE_SITE_URL=http://localhost:12345
      - INVENTREE_DB_ENGINE=sqlite
      - INVENTREE_DB_NAME=/home/inventree/data/test_db.sqlite3
      - INVENTREE_ADMIN_USER=testuser
      - INVENTREE_ADMIN_PASSWORD=testpassword
      - INVENTREE_ADMIN_EMAIL=test@test.com
    volumes: ["./data:/home/inventree/data"]
```

Steps taken from the client repo's `tasks.py`:

```
docker compose run --rm inventree invoke update --skip-backup --no-frontend --skip-static
docker compose run --rm inventree invoke dev.delete-data -f
docker compose run --rm inventree invoke migrate
docker compose run --rm inventree invoke dev.import-fixtures
docker compose up -d
```

- **Admin user**: `INVENTREE_ADMIN_USER`, `INVENTREE_ADMIN_PASSWORD` (or `_PASSWORD_FILE`),
  `INVENTREE_ADMIN_EMAIL` create a superuser on startup. Interactive alternative: `invoke superuser`.
- **Token**: `curl -u testuser:testpassword "http://localhost:12345/api/user/me/token/?name=ci"`.
- **Readiness**: poll the token endpoint or `/api/` until it answers 200.
- **SQLite**: supported (`sqlite3`, minimum SQLite 3.31.0, WAL mode on by default) but "not recommended
  for production use, and should only be used for testing or development purposes". It will not
  reproduce PostgreSQL locking or length-constraint behaviour, so keep one PostgreSQL job.
- **No worker** in this setup: events, notifications and background tasks will not run
  (`worker_running` false). Fine for adjustment and read-back tests.
- **`INVENTREE_DEBUG=True` disables throttling**; test 429 handling with a mock instead.
- **Test data**: `invoke dev.import-fixtures` loads the server's own unit-test fixtures.
  `invoke dev.setup-test -i` loads the demo dataset from `inventree/demo-dataset`
  (`inventree_data.json` plus media) and "will delete all existing data". Demo accounts include
  `admin`/`inventree`, `allaccess`/`nolimits`, `engineer`/`partsonly`, `reader`/`readonly`, which are
  handy for permission tests. For deterministic tests, seeding a small purpose-built dataset over the
  API (Acme Fiber parts, Sam's truck location) is likely better than depending on demo pks.
- **Cold start time**: [unverified]. Only data point: the client repo waits up to 60 s for the server
  after `up -d`, and its CI uses `invoke wait`. Migrations on a fresh database add more; measure it.

### How inventree-python's own CI does it

Its GitHub Actions workflow does **not** use Docker. It clones `inventree/inventree` master, runs
`invoke install`, `invoke migrate`, `invoke dev.import-fixtures`, starts `invoke dev.server -a
0.0.0.0:12345` in the background, runs `invoke wait`, then runs unittest against
`http://localhost:12345` with SQLite and `INVENTREE_ADMIN_*` env vars. Docker compose is used only for
local runs (`invoke test`). It tests against master only, not a version matrix.

Sources:
- https://docs.inventree.org/en/stable/start/docker/ , https://docs.inventree.org/en/stable/start/docker_install/
- https://docs.inventree.org/en/stable/start/config/ , https://docs.inventree.org/en/stable/demo/
- https://hub.docker.com/r/inventree/inventree/tags
- https://github.com/inventree/InvenTree/tree/stable/contrib/container
- https://github.com/inventree/inventree-python/blob/master/test/docker-compose.yml
- https://github.com/inventree/inventree-python/blob/master/tasks.py
- https://github.com/inventree/inventree-python/blob/master/.github/workflows/ci.yaml
- https://github.com/inventree/inventree-python/blob/master/TESTING.md
- https://github.com/inventree/demo-dataset

## 11. License

| Project | License |
|---|---|
| InvenTree server | MIT |
| inventree-python | MIT |
| demo-dataset | MIT |

- Talking to InvenTree over REST creates no licensing obligation for the connector; it can use any license.
- Importing `inventree` only requires keeping the MIT copyright and permission notice if the client is
  redistributed (for example vendored or bundled in an image).
- A plugin would import InvenTree server modules; still MIT, so no copyleft concern.
- Copying code or fixtures from either repo into the connector requires retaining the MIT notice.
- This is a reading of the licenses, not legal advice.

Sources:
- https://github.com/inventree/InvenTree/blob/master/LICENSE
- https://github.com/inventree/inventree-python , https://pypi.org/project/inventree/
- https://github.com/inventree/demo-dataset

---

## Open questions / unverified

1. **Over-quantity and depletion behaviour on 1.5.6.** Read from 1.0.0 source; PRs since then did not
   appear to change it, but I could not read the 1.5.6 `stock/models.py`. Needs an integration test:
   transfer 5 from an item holding 3; remove 5 from an item holding 3.
2. **Response body of adjustment endpoints.** 201 is confirmed by a server unit test; the body and
   whether it ever exposes the new stock item pk are not.
3. **400 error body shape** for per-item validation errors.
4. **Required role for adjustments** (Stock Item "add") is inferred from permission code, not tested.
   Effect of "Stock Ownership Control" is unknown.
5. **Exact deltas on the `SPLIT_FROM_PARENT` entry** (how quantity and location are recorded).
6. **Notes longer than 512 chars**: the serializer has no max length but the model does. Likely a
   database error on PostgreSQL; untested.
7. **Concurrency**: whether two simultaneous transfers from the same stock item can oversell.
   No row locking was seen in `move()` in 1.0.0; 1.5.0 added pk-ordered processing.
8. **Token re-issue-by-name and one-time display** confirmed for master and latest docs, not for 1.5.x.
9. **`Bearer` prefix** acceptance by the token authentication class.
10. **Metadata and tags on tracking entries**: appear absent; not confirmed against the model list.
11. **API changelog version numbers** for entries 288/289, 111/112 and the StockLocation parameters
    entry: two reads disagreed. PR links are correct; numbers may be off by one.
12. **API version at exactly 1.4.0 and 1.5.0** (values were read from 1.4.4 and the `stable` branch).
13. **Support policy**: no written policy found; "only the latest minor gets patches" is inferred
    from release dates.
14. **Cold start time** of the container; not measured.
15. **Part and stock list filters on stable** were read from master; assumed identical.
16. **Unpaginated list response shape** when `limit` is omitted.
17. **Slack plugin credential type**: docs summary said API key, source shows a URL setting.
18. **Python 3.9 to 3.11 support of the client** is declared on PyPI but CI only exercises 3.12.
