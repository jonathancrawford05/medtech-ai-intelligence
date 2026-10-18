# LLM extraction fixtures (PR 4D)

A real slice of the 4D spike ([finding 0023](../../../findings/0023-local-llm-extraction-spike.md)),
so the 4D code can be built and tested without Ollama or the lakehouse.

| File | What it is |
|---|---|
| `documents/<K>.json` | The Summary pages 4B located (`summary_pages`) for the 41 documents in the gold set plus 3 failure-mode extras (below), as `{page, text}`, exactly as sent to the model. Text is pypdf output from `bronze_summary_documents`; artefacts (`75 .9%`, split words, a cipher-font page in `K182456`) are kept on purpose. |
| `responses/<K>.json` | The recorded `qwen3:14b` response for that document (`content` is the raw JSON string the model returned), with model digest, prompt version, options and token counts. |
| `prompt_4d-spike-4.json` | The system prompt and JSON schema that produced the responses. |
| `gold_set_v1.csv` | 58 extracted values with a judgement each (`y` / `n` / `partly` / `unsure`), the correct value, and the evidence page and quote. Labelled by an independent Claude agent against the full text, blind to check status and sampling reason (`review_method=llm_assisted`), then reviewed and accepted by the maintainer on 2026-10-18. |

Failure-mode extras (not in the gold set; no labels, but each has one property a test needs):

| Document | Why it is here |
|---|---|
| `K251218` | A test list without results: the model invented five ~98% "other" metrics whose quotes contain no numbers. Every one must be rejected (`value_not_in_quote`). |
| `K182456` | Both Summary pages are a substitution-cipher text layer (`1OP?T\x015<O>C…`, control characters for spaces) that 4A classed as `text`. The garble test must flag both pages before any model call; the recorded response (three 98.1% metrics) must not survive. |
| `K230842` | A runaway generation: 78 metric rows, 9,258 output tokens, 58 of them exact duplicates. Tests for the output cap and the duplicate rule. |

The eCART image-table fabrication (`K233253`) is in the gold set itself.

Sampling: seed 20261018, over-weighting likely errors (see `why_sampled`), so per-row
accuracy here is not a population rate; finding 0023 gives the weighted estimate.

The model was run on the maintainer's Mac (Ollama, temperature 0, seed 42, `think: false`,
`num_ctx` 16384). Do not regenerate these files in CI; record new ones on the Mac.
