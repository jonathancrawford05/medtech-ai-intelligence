"""LLM-assisted study-evidence extraction from 510(k) Summaries (Issue 4, PR 4D; ADR 0022).

A local model (Ollama) proposes values, each with a verbatim quote; deterministic
code decides which survive (`checks`). Nothing here forms a judgement: every row is
evidence with ``review_method="llm_assisted"``, and nothing here touches a mortality
flag (ADR 0007, ADR 0014).

``schema``    what the model is asked for (Pydantic -> JSON schema in Ollama's ``format``)
``pages``     which pages it sees (4B's located pages, minus garbled ones)
``provider``  the thin model interface: output cap, retry once, then skip
``checks``    the quote/value ladder that accepts, flags or rejects every value
``run``       bronze_llm_responses (append-only) -> silver_llm_evidence (rebuilt)
``gold``      scores a run against a labelled gold set, per field and per check status
"""
