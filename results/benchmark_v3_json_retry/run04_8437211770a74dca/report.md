# Research report

**Question:** What changed technically between Llama 2 and Llama 3?

## Summary

Llama 3 introduces only modest architectural tweaks to the dense transformer backbone of Llama 2, expands its context window, and dramatically enlarges its training corpus. [^1][^2]

## Architectural Modifications

Llama 3 retains the dense transformer backbone of Llama 2, with only minimal architectural changes reported. [^1]

## Training Data Expansion

The Llama 3 training set exceeds 15 trillion tokens, more than seven times the size of Llama 2’s dataset. [^2][^3]

## Context Window Increase

Llama 3’s context window was enlarged to 8,192 tokens, doubling the 4,000‑token window of Llama 2. [^2]

## Limitations

- Parameter counts for each variant are not provided in the available findings.
- Inference performance metrics \(latency, FLOPs, throughput\) are not reported.
- Unanswered: How many parameters does each Llama 2 and Llama 3 variant have and what are the parameter count differences? (search budget reached: research status: no\_results)
- Unanswered: What are the reported inference performance metrics \(latency, FLOPs, throughput\) for Llama 3 versus Llama 2? (send-back limit reached: research status: no\_results)

## Sources

[^1]: [The Llama 3 Herd of Models](https://arxiv.org/html/2407.21783v3) (arxiv.org, retrieved 2026-10-17). Findings F1: “While Llama 3 makes minimal architectural modifiations to compared to Llama 2, other recent foundation models have explored other designs.”
[^2]: [Llama 2 to Llama 3: Key Upgrades and What Changed](https://www.bluetickconsultants.com/exploring-the-evolution-llama-2-to-llama-3/) (bluetickconsultants.com, retrieved 2026-10-17). Findings F4, F2: “with context length of 8192 tokens, supports 30 languages.”
[^3]: [LLAMA 2 Vs LLAMA 3 - Blockchain Council](https://www.blockchain-council.org/ai/llama-2-vs-llama-3/) (blockchain-council.org, retrieved 2026-10-17). Findings F3: “Training \(link removed\) The training data for LLAMA 3 is over seven times larger than that used for LLAMA 2, with more than 15 trillion tokens.”
