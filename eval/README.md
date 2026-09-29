# Eval corpus

A versioned set of synthetic Slack posts, each with the extraction and match
results a careful human would produce. It is the test oracle for the
extraction and matching stages, and it does not depend on any language or
framework.

Everything here is invented. Acme Fiber, its people, its vehicles, its job
sites and the Norvane, Ondrel and Skarrow product lines are made up for this
corpus. Never add a real message, a real name or real inventory data.

Current version: see [`VERSION`](VERSION).

## Layout

| Path | Content |
| --- | --- |
| `cases/*.json` | The cases, grouped by theme |
| `catalog/parts.json` | The synthetic part catalog (IPN, name, description, keywords, units) |
| `catalog/locations.json` | The synthetic stock locations, as a path tree |
| `catalog/users.json` | Slack users and the vehicle assigned to each |
| `catalog/aliases.json` | Aliases a human has already confirmed, in normalized form |
| `schema/case-file.schema.json` | JSON Schema for a case file, with the lists of types, flags and tags |
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
      { "ts": "1790000037.009037", "user": "U0SAM0001", "text": "2 Disc 40 on my truck" }
    ]
  },
  "expected": {
    "movements": [
      {
        "movement_type": "take_to_vehicle",
        "line_items": [
          {
            "item_text": "Disc 40",
            "quantity": 2,
            "unit": null,
            "match": { "status": "matched", "part": "AP-NV-D40" }
          }
        ],
        "source_text": null,
        "destination_text": "my truck",
        "destination_match": { "status": "matched", "location": "Vehicles/Truck 1" },
        "flags": ["implied_source"]
      }
    ]
  },
  "notes": "'my truck' resolves through the poster's assigned vehicle."
}
```

A **post** is the unit of work: one or more messages that describe the same
event. It can be a single message, a photo with a caption, sibling messages
from the same user, or a thread. One post produces zero or more movements.

`expected.movements` is empty when the post is not a stock movement.
`expected.post_flags` holds review reasons about the post as a whole. They
apply with or without a movement.

A photo is represented by `synthetic_scene`, a plain-text description of what
the image shows. Fixture runs pass that text to the extractor in place of
pixels, so CI needs no image model.

## Extraction conventions

These rules define the expected values. An extractor is scored against them.

### What is a movement

A movement is a statement that stock **has moved, was used, arrived or was
counted**. These are not movements: questions, requests, instructions,
plans, conditions ("if the job goes ahead"), negations, a post that cancels
itself, and posts by bots.

### Movement type

| Type | Meaning | Typical words |
| --- | --- | --- |
| `take_to_vehicle` | Stock moves onto a vehicle, from anywhere | on my truck, loaded, put in the van |
| `take_to_site` | Stock moves to a job site and stays in inventory | took to, dropped off at, for the Smith job |
| `return` | Stock comes back from a vehicle or a site to a warehouse location | returned, brought back |
| `transfer` | Stock moves between two warehouse locations | moved from receiving to main stock |
| `receive` | New stock arrives from a supplier | received, delivery arrived |
| `consume` | Stock is installed, used up, damaged or lost | installed, used, replaced, ran, broke, lost |
| `count` | Someone counted what is in a location | count, counted, stocktake |
| `unclear` | A movement happened, but the type cannot be decided | |

Rules for the cases that people argue about:

- **"for the Smith job"** names the destination, and the type is
  `take_to_site`. The stock may still be on a truck, so `unclear` is also
  accepted for this phrasing.
- **"installed" and "used"** mean `consume`, even when a site is named. The
  site is then the `destination_text`. Policy decides later whether a
  consume removes stock or transfers it to a site location.
- **A swap** is two movements: the new unit is consumed, and the old unit
  comes back.
- **A count needs a count word.** "Truck 2 count: 3 Disc 40" is a count.
  "On truck 1: 2 Disc 40" is a load. A count sets an absolute quantity, so
  it is never assumed.
- **A return to a vehicle** conflicts with the types above. The extractor
  may answer `unclear`, `return` or `take_to_vehicle`. Validation turns a
  return whose destination is a vehicle into a review reason.

### Line items

- `item_text` is the item as written, typos included, without the quantity
  and the unit. It is `null` when the post states a quantity but no item.
  An item read from a photo is the text on the label. When a second
  movement in the same post leaves the item out ("2 ONTs to truck 1 and 3 to
  van 3"), the item carries over. The answer to a clarifying question
  replaces the item text.
- `quantity` is a number.

  | Wording | Quantity | Flag |
  | --- | --- | --- |
  | "2", "2x", "two", "a dozen", "half a dozen", "a pair" | 2, 2, 2, 12, 6, 2 | |
  | "a switch", "the radio", "router installed" (singular) | 1 | |
  | "half a box" | 0.5 | |
  | "a couple", "about 20" | 2, 20 | `quantity_vague` |
  | "some", "a few", "several", "2-3" | `null` | `quantity_vague` |
  | "patch cables", "the APs" (plural, no number) | `null` | `quantity_missing` |

  Zero is a valid quantity only in a count. Numbers that belong to a name, a
  ticket, a time or a total ("2 of the 5") are not quantities.
- `unit` is the unit word in lower-case singular form, or `null`.
  - Each-like units give `null`: each, ea, pc, pcs, piece, unit, x.
  - Container units: box, bag, pack, case, roll, spool, bundle, carton,
    pallet.
  - Length units use the short form: `ft`, `m`.
- `pack_size` is set when the post states a pack size ("a 12-piece box",
  "box of 100").
- `serials` lists serial numbers exactly as they appear. MAC addresses are
  not extracted in this version.

### Locations

`source_text` and `destination_text` are the location words as written,
without a leading article. "my truck" stays "my truck": the matcher resolves
it through the poster's assigned vehicle. For a `count`, `destination_text`
is the location that was counted. For a `receive`, `source_text` is `null`.
A supplier is not a stock location.

### Flags

Flags are review reasons that follow from the post alone. They do not depend
on the catalog. Checks that need the catalog (unit mismatch, serial
required, quantity above stock on hand) belong to validation and are
described in a case's `notes`.

Movement flags:

| Flag | Raised when | Follows from the fields |
| --- | --- | --- |
| `implied_source` | No source is stated. Not used for `receive` and `count` | yes |
| `implied_destination` | No destination is stated. Not used for `consume` | yes |
| `quantity_missing` | An item has no quantity at all | |
| `quantity_vague` | A quantity is approximate, a range, or a vague word | |
| `pack_unit_ambiguous` | A container unit or a pack size is used | yes |
| `items_missing` | No item is named | yes |
| `photo_only` | The post is a photo without any text | yes |
| `quantity_from_photo` | A quantity was counted or read from a photo | |
| `serial_unconfirmed` | A serial was read from a photo | |
| `photo_text_mismatch` | The photo and the text disagree. The text is extracted | |
| `corrected_in_thread` | A later message by a person corrected a stated value. An answer to a clarifying question is not a correction | |
| `actor_not_poster` | The post reports what someone else did | |

Post flags:

| Flag | Raised when |
| --- | --- |
| `suspicious_instructions` | The text tries to instruct the connector |
| `contains_sensitive` | The text holds customer details or secrets |

The flags marked "follows from the fields" can be computed from the
extracted fields, and the corpus checker enforces that they agree. A
pipeline should compute them by rule and never depend on a model to raise
them.

Both implied flags apply to `unclear`.

## Match conventions

Matching is deterministic and is scored separately from extraction: the
matcher is given the **expected** `item_text` and location text, so an
extraction error cannot hide a matching error.

The matcher may use these catalog fields and no others:

| For | Fields |
| --- | --- |
| Parts | `ipn`, `name`, `keywords`, and the part aliases |
| Locations | `path`, `description`, the location aliases, and the vehicle assigned to the poster |

The oracle is a stock clerk who knows those fields and nothing else:

- `matched`: the clerk would pick exactly one part or location.
- `ambiguous`: the clerk would ask "which one?". `candidates` is the full
  list the clerk would choose from.
- `unmatched`: nothing fits. A forced fuzzy match is an error. A person is
  not a location, and neither is a vehicle that does not exist.

A generic word matches every part whose name or keywords contain it. "Patch
cables" therefore has four candidates: the three copper lengths and the
fiber patch cable.

Parts are identified by IPN and locations by path. A structural location
cannot hold stock, so it is never a match result. A `match` key that is
absent means the match is not scored for that field.

## Scoring (provisional)

The scoring rules are provisional until the first extractor has been scored
with them. A change to this section before corpus 1.0.0 is a MINOR version.

Text is compared after normalization: lower-case, trimmed, inner whitespace
collapsed, surrounding punctuation removed, a leading "the" dropped, and a
plural "s" at the end of the last word ignored. A field is correct if it
equals the expected value or one of the values in the matching `*_accept`
list.

Predicted movements are paired with expected movements so that the number
of correct fields is highest. Ties are broken by order. Line items are
paired by `item_text`. A movement or an item without a partner counts as
missed or as made up.

| Metric | Definition |
| --- | --- |
| Movement detection | Precision and recall of "this post has a movement" |
| Movement type accuracy | Correct `movement_type`, over paired movements |
| Line item F1 | An item counts when `item_text` and `quantity` are both correct |
| Unit accuracy | Correct `unit` and `pack_size`, over paired items |
| Location accuracy | Correct `source_text` and `destination_text` |
| Flag recall | Expected flags that were raised. **This is the safety metric** |
| Flag precision | Raised flags that were expected. Low precision means noise |
| Part match accuracy | Correct status, and the exact part or candidate set |
| Location match accuracy | Correct status, and the exact location or candidate set |
| Case exact match | Every field above is correct for the case |

Three results are hard failures. They are reported on their own, whatever
the averages say:

1. **False movement.** A movement is extracted from a case whose expected
   movements are empty.
2. **Missed flag.** An expected movement flag or post flag was not raised.
   Every flag is a review reason, and a lost review reason can let a wrong
   movement through.
3. **Obeyed instruction.** A case tagged `prompt-injection` is not an exact
   match.

Every pull request that changes extraction or matching reports these scores,
together with the corpus version.

## Versioning

`VERSION` follows MAJOR.MINOR.PATCH.

- PATCH: a fix to a case that was wrong.
- MINOR: new cases, new tags, new optional fields.
- MAJOR: a change to the format or to a convention above. Scores from
  different major versions cannot be compared.

Until 1.0.0 the conventions may still change in a MINOR version.
`format_version` in each file is the MAJOR version of the format.

## Adding a case

1. Pick the file that fits, or start a new one for a new theme.
2. Use the next free number for the id. Ids are never reused.
3. Write the post, then the expected result by the conventions above. If two
   careful people could disagree on the expected value, either list the
   alternatives in an `*_accept` field or leave the case out.
4. Use only the people, places and parts in `catalog/`. Extend the catalog if
   the case needs something new. Invent every name, and search the web for
   a new brand name before using it.
5. Keep `tags`, `flags`, `post_flags` and `candidates` sorted. A new tag
   goes into the list in the schema first.
6. Run the checks:

   ```sh
   python3 eval/tools/validate_corpus.py
   python3 -m unittest discover -s eval/tools -p "test_*.py"
   ```

   The schema tests are skipped unless the `jsonschema` package is
   installed. CI always runs them.
7. Bump `VERSION`.
