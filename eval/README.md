# Eval corpus

A versioned set of synthetic Slack posts, each with the extraction and match
results a careful human would produce. It is the test oracle for the
extraction and matching stages, and it does not depend on any language or
framework.

Everything here is invented. Acme Fiber, its people, its vehicles, its job
sites and the Norvane, Brightlink and Skyreach product lines do not exist.
Never add a real message, a real name or real inventory data.

Current version: see [`VERSION`](VERSION).

## Layout

| Path | Content |
| --- | --- |
| `cases/*.json` | The cases, grouped by theme |
| `catalog/parts.json` | The synthetic part catalog (IPN, name, description, units) |
| `catalog/locations.json` | The synthetic stock locations, as a path tree |
| `catalog/users.json` | Slack users and the vehicle assigned to each |
| `catalog/aliases.json` | Aliases a human has already confirmed |
| `schema/case-file.schema.json` | JSON Schema for a case file |
| `tools/validate_corpus.py` | Corpus checker, standard library only |

## A case

```json
{
  "id": "clear-001",
  "tags": ["brief-example"],
  "post": {
    "channel": "C0ACME001",
    "user": "U0SAM0001",
    "messages": [
      { "ts": "1790000037.001037", "user": "U0SAM0001", "text": "2 L7 Lite on my truck" }
    ]
  },
  "expected": {
    "movements": [
      {
        "movement_type": "take_to_vehicle",
        "line_items": [
          {
            "item_text": "L7 Lite",
            "quantity": 2,
            "unit": null,
            "match": { "status": "matched", "part": "AP-NV-L7" }
          }
        ],
        "source_text": null,
        "destination_text": "my truck",
        "destination_match": { "status": "matched", "location": "Vehicles/Truck 1" },
        "flags": ["implied_source"]
      }
    ]
  }
}
```

A **post** is the unit of work: one or more messages that describe the same
event. It can be a single message, a photo with a caption, sibling messages
from the same user, or a thread. One post produces zero or more movements.

`expected.movements` is empty when the post is not a stock movement.

A photo is represented by `synthetic_scene`, a plain-text description of what
the image shows. Fixture runs pass that text to the extractor in place of
pixels, so CI needs no image model. `image_path` is reserved for synthetic
images used in live vision runs.

## Extraction conventions

These rules define the expected values. An extractor is scored against them.

**Movement type**

| Type | Meaning |
| --- | --- |
| `take_to_vehicle` | Stock moves onto a vehicle, from anywhere |
| `take_to_site` | Stock moves to a job site and stays in inventory |
| `return` | Stock comes back to a warehouse location |
| `receive` | New stock arrives from a supplier |
| `consume` | Stock is installed, used up, damaged or lost |
| `count` | Someone counted what is in a location |
| `unclear` | A movement happened, but the type cannot be decided |

Questions, requests, plans, negations and chatter are not movements.
"Installed" and "used" mean `consume`, even when a site is named. The site is
then the `destination_text`. Policy decides later whether a consume removes
stock or transfers it to a site location.

**Line items**

- `item_text` is the item as written, typos included, without the quantity
  and the unit. It is `null` when the post states a quantity but no item.
- `quantity` is a number. "a", "an" and "the" with a singular noun mean 1.
  Spelled-out numbers are converted ("three" is 3, "a dozen" is 12, "half"
  is 0.5). Vague words ("some", "a few") and ranges ("2-3") give `null`.
  "A couple" gives 2 and the `quantity_vague` flag. "About 20" gives 20 and
  the same flag.
- `unit` is the unit word in lower-case singular form (`box`, `bag`, `ft`).
  It is `null` when no unit is given or when the unit means each ("pcs",
  "pieces", "ea", "x").
- `pack_size` is set when the post states a pack size ("a 12-piece box",
  "box of 100").
- `serials` lists serial numbers exactly as they appear.

**Locations**

`source_text` and `destination_text` are the location words as written,
without a leading article. "my truck" stays "my truck": the matcher resolves
it through the poster's assigned vehicle. For a `count`, `destination_text`
is the location that was counted.

**Flags**

Flags are review reasons that follow from the post alone. They do not depend
on the catalog. Checks that need the catalog (unit mismatch, serial required,
quantity above stock on hand) belong to validation and are described in a
case's `notes`.

| Flag | Raised when |
| --- | --- |
| `implied_source` | No source is stated. Not used for `receive` and `count` |
| `implied_destination` | No destination is stated |
| `quantity_missing` | An item has no quantity at all |
| `quantity_vague` | A quantity is approximate, a range, or a vague word |
| `pack_unit_ambiguous` | A container unit or a pack size is used |
| `items_missing` | No item is named |
| `photo_only` | The post is a photo without any text |
| `quantity_from_photo` | A quantity was counted from a photo |
| `serial_unconfirmed` | A serial or MAC was read from a photo |
| `photo_text_mismatch` | The photo and the text disagree |
| `corrected_in_thread` | A later message changed the movement |
| `actor_not_poster` | The post reports what someone else did |
| `contradictory` | The post contradicts itself |
| `suspicious_instructions` | The text tries to instruct the connector |
| `contains_sensitive` | The text holds customer details or secrets |

## Match conventions

Matching is deterministic and is scored separately from extraction. The
oracle is a stock clerk who knows the catalog and the confirmed aliases, and
nothing else:

- `matched`: the clerk would pick exactly one part or location.
- `ambiguous`: the clerk would ask "which one?". `candidates` is the full
  list the clerk would choose from.
- `unmatched`: nothing fits. A forced fuzzy match is an error.

Parts are identified by IPN and locations by path. A structural location
cannot hold stock, so it is never a match result. A `match` key that is
absent means the match is not scored for that field.

## Scoring

Text is compared after normalization: lower-case, trimmed, inner whitespace
collapsed, surrounding punctuation removed, and a leading "the" dropped. A
field is correct if it equals the expected value or one of the values in the
matching `*_accept` list.

Movements are paired with expected movements in order, and line items are
paired by best normalized `item_text` match.

| Metric | Definition |
| --- | --- |
| Movement detection | Precision and recall of "this post has a movement" |
| Movement type accuracy | Correct `movement_type`, over detected movements |
| Line item F1 | An item counts when `item_text` and `quantity` are both correct |
| Unit accuracy | Correct `unit` and `pack_size`, over paired items |
| Location accuracy | Correct `source_text` and `destination_text` |
| Flag recall | Expected flags that were raised. **This is the safety metric** |
| Flag precision | Raised flags that were expected. Low precision means noise |
| Part match accuracy | Correct status, and the exact part or candidate set |
| Location match accuracy | Correct status, and the exact location or candidate set |
| Case exact match | Every field above is correct for the case |

Two results are hard failures and are reported on their own, whatever the
averages say:

1. **False movement.** A movement is extracted from a `negative-*` case.
2. **Missed safety flag.** A movement lacks an expected flag and has no
   other flag, so it would look clean to the policy step.

Every pull request that changes extraction or matching reports these scores,
together with the corpus version.

## Versioning

`VERSION` follows MAJOR.MINOR.PATCH.

- PATCH: a fix to a case that was wrong.
- MINOR: new cases, new tags, new optional fields.
- MAJOR: a change to the format or to a convention above. Scores from
  different major versions cannot be compared.

`format_version` in each file is the MAJOR version of the format.

## Adding a case

1. Pick the file that fits, or start a new one for a new theme.
2. Use the next free number for the id. Ids are never reused.
3. Write the post, then the expected result by the conventions above. If two
   careful people could disagree on the expected value, either list the
   alternatives in an `*_accept` field or leave the case out.
4. Use only the people, places and parts in `catalog/`. Extend the catalog if
   the case needs something new.
5. Run the checks:

   ```sh
   python3 eval/tools/validate_corpus.py
   python3 -m unittest discover -s eval/tools -p "test_*.py"
   ```

6. Bump `VERSION`.
