# LLM extraction fixtures (PR 4D)

A real slice of the 4D spike ([finding 0023](../../../findings/0023-local-llm-extraction-spike.md)),
so the 4D code can be built and tested without Ollama or the lakehouse.

| File | What it is |
|---|---|
| `documents/<K>.json` | The Summary pages 4B located (`summary_pages`) for the 41 documents in the gold set, as `{page, text}`, exactly as sent to the model. Text is pypdf output from `bronze_summary_documents`; artefacts (`75 .9%`, split words, a cipher-font page in `K182456`) are kept on purpose. |
| `responses/<K>.json` | The recorded `qwen3:14b` response for that document (`content` is the raw JSON string the model returned), with model digest, prompt version, options and token counts. |
| `prompt_4d-spike-4.json` | The system prompt and JSON schema that produced the responses. |
| `gold_set_v1.csv` | 58 extracted values with a judgement each (`y` / `n` / `partly` / `unsure`), the correct value, and the evidence page and quote. Labelled by an independent Claude agent against the full text, blind to check status and sampling reason (`review_method=llm_assisted`), then reviewed and accepted by the maintainer on 2026-10-18. |

Sampling: seed 20261018, over-weighting likely errors (see `why_sampled`), so per-row
accuracy here is not a population rate; finding 0023 gives the weighted estimate.

The model was run on the maintainer's Mac (Ollama, temperature 0, seed 42, `think: false`,
`num_ctx` 16384). Do not regenerate these files in CI; record new ones on the Mac.
