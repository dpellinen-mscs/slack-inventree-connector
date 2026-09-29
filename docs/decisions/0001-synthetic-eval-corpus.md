# 0001: The eval corpus is fully synthetic and stack-independent

- Status: Accepted
- Date: 2026-09-28
- Decided by: agent (FYI to Dan)

## Context

Extraction and matching need a test oracle before any code exists, and the
stack is not decided yet. The project is a clean-room build, so no real
message, name or inventory record may enter the repository. Real vendor and
product names would also make the corpus look like somebody's actual stock
list.

## Decision

- The corpus is plain JSON with a JSON Schema. Its checker uses only the
  Python standard library, so it runs before and after the stack decision.
- Every company, person, site, vendor and product in it is invented: Acme
  Fiber, and the Norvane, Ondrel and Skarrow product lines.
- A photo is represented by `synthetic_scene`, a text description of the
  image. Fixture runs use that text in place of pixels. Synthetic images for
  live vision runs can be added later through `image_path`.
- Expected values follow written conventions (`eval/README.md`). A case
  whose expected value is open to dispute lists the accepted alternatives or
  is left out.
- Brand names were searched on the web before use, and none belongs to a
  networking or telecom vendor. Model names do not mirror a real product
  line.
- Flags in the corpus are text-level review reasons only. Checks that need
  the catalog belong to validation.
- Three results are hard failures whatever the averages say: a movement
  extracted from a post that has none, a missed flag, and a
  prompt-injection case that is not an exact match.

## Consequences

- Invented brands are harder for an LLM than famous ones, because the model
  cannot lean on product knowledge. That is intended: the model never picks
  the part, and real catalogs are full of names a model has never seen.
- Text stand-ins do not measure vision quality. Photo extraction stays
  outside the v1.0 quality claims until real synthetic images exist.
- The conventions are now a compatibility surface. Changing one is a major
  corpus version, and scores across major versions cannot be compared.
