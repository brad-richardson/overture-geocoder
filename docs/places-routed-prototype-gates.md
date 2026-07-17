# Routed Places prototype gates

Status: measurement contract; Places remains disabled by default.

## Routing and head eligibility

An explicit catalog context or a point covered by the catalog routes to exactly
one immutable compact shard. A routed miss returns empty and never enumerates
neighboring or global shards. A catalog miss returns a bounded error after the
catalog read and performs no shard read.
Context and point are mutually exclusive so a request cannot route by one and
rank or report distance from contradictory location evidence.

The packed head is the only context-free path in this prototype. It accepts one
or two exact, unfielded normalized tokens. A two-token query first consults the
famous pair entry (`e2:{a} {b}`, tokens in ascending order), which is the
builder's bounded top-10 of the exact posting intersection for pairs drawn from
the capped famous set; on a pair miss it falls back to the stable ID
intersection of the per-token packed top-10 entries, and every token must then
have a packed entry. Single-token queries are unchanged; famous-set name/brand
tokens may be admitted below the density floor, with entry semantics identical
to dense entries. Prefix, fielded, and three-or-more-token queries require
explicit location. A packed entry is acceleration evidence, not relevance
evidence: “famous” additionally requires the labelled context-free case to put
the named feature in the top five with stable order. Frequency alone cannot
pass that gate, and neither can presence in the famous set: the fame proxy is
quantized confidence, and the labelled relevance panel remains the arbiter.

## Measurement method

Every latency case uses a separate immutable R2 object namespace. Attempt one
must contain R2 reads; later attempts measure the same namespace and report
cache reads separately. Record client time, Worker `Server-Timing`, logical and
physical ranges, R2/cache reads and bytes, routed shard, result projection, and
repeat order. The catalog-failure case also has an independent namespace.

## Initial decision gates

Classify the run `proceed` only when all of the following hold:

- every located request reads at most one shard and every catalog miss reads
  none;
- independently cold located requests use at most eight physical R2 reads,
  transfer at most 512 KiB, and complete within 1.0 second client time;
- cold packed-head requests use at most four physical R2 reads, transfer at
  most 256 KiB, and complete within 1.0 second client time;
- warm median client time is at most 250 ms for each route class and warm
  samples perform zero R2 reads after propagation;
- each labelled relevance class has a relevant result in the top five;
- no out-of-context result outranks an in-context equivalent;
- returned feature IDs contain no duplicates, while distinct branches of the
  same chain remain distinct; and
- all repeated result orders are identical.

Classify `optimize` when correctness and relevance pass but a byte, read, or
latency gate fails. Classify `stop` when any normal located query fans out, any
relevance class has zero relevant top-five results, context ordering is wrong,
or repeat order is unstable. None of the three classifications enables the
public `place` type by itself; `proceed` only authorizes the next producer and
failure-semantics slice.

The workflow emits an automated technical classification of `optimize` or
`awaiting_relevance_benchmark` and always sets `launch_approved` to false. Workflow
success means the bounded measurement and cleanup completed; it is never a
`proceed` decision. After inspecting the returned projections, a human records
the final `proceed`, `optimize`, or `stop` classification in the retained
evidence report. The review should use a side-by-side top-five panel from the
previously evaluated reference geocoders, including Nominatim, rather than
unaided inspection. Reference engines are comparators rather than ground truth:
provider disagreements still need adjudication for coverage, local names,
categories, and requested context.

Distance is reported for routed results but is diagnostic only. The compact
reader selects a bounded confidence/doc-ID result window before decoding result
coordinates, so reordering that window would not be a complete nearest-result
ranking. The `category_near_me` label therefore cannot justify launch until a
bounded ranking component can compare every eligible candidate; absent that,
the final classification is at best `optimize`.
