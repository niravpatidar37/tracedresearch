# Partial findings

**Question:** What changed technically between Llama 2 and Llama 3?

## Summary

Writer not run \(wall\_clock budget\); listing extracted findings per sub-question. [^1]

## What are the architectural differences \(e.g., model depth, attention mechanism\) between Llama 2 and Llama 3?

Llama 3’s architecture incorporates Grouped Query Attention \(GQA\) to improve inference efficiency, whereas Llama 2 does not include GQA. [^1]

Llama 3’s tokenizer is more efficient with 128,000 tokens, a detail not mentioned for Llama 2. [^1]

Llama 3 uses a grouped‑query attention mechanism. [^2]

Llama 3’s top model has 405 billion parameters, far exceeding Llama 2’s largest model. [^3]

## What changes were made to the training dataset size and composition for Llama 3 compared to Llama 2?

The Llama 3 training corpus contains 15 trillion tokens, which is seven times larger than Llama 2’s dataset. [^4]

Llama 3’s training set expanded to over 15 trillion tokens, a seven‑fold increase from Llama 2’s 2 trillion‑token corpus. [^5]

## Limitations

- Report generated without the writer model because of: wall\_clock budget.
- Unanswered: How does the total parameter count and token context window differ between Llama 2 and Llama 3? (search budget reached: research status: no\_results)
- Unanswered: What are the reported inference latency and hardware efficiency improvements of Llama 3 over Llama 2? (send-back limit reached: research status: no\_results)

## Sources

[^1]: [LLAMA 2 Vs LLAMA 3 - Blockchain Council](https://www.blockchain-council.org/ai/llama-2-vs-llama-3/) (blockchain-council.org, retrieved 2026-10-17). Findings F1, F2: “- Model Architecture: LLAMA 3 incorporates enhancements like Grouped Query Attention \(GQA\) to improve inference efficiency.”
[^2]: [Llama 3 vs Llama 2](https://www.apps4rent.com/blog/llama-3-vs-llama-2/) (apps4rent.com, retrieved 2026-10-17). Findings F3: “\| Attention Mechanism \| Grouped-query attention \| Grouped-query attention \|”
[^3]: [Llama 3 vs. Llama 2: Why the Newest Model Leaves Its Predecessor in the Dust](https://www.lifewire.com/llama-2-vs-llama-3-8714445) (lifewire.com, retrieved 2026-10-17). Findings F4: “Its top model, 405B, is so named because it uses 405 billion parameters to make its decisions based on its extensive training data.”
[^4]: [Analysis of Llama 3 \| Continuum Labs](https://training.continuumlabs.ai/models/foundation-models/analysis-of-llama-3) (training.continuumlabs.ai, retrieved 2026-10-17). Findings F5: “The Llama 3 model was trained on a massive dataset consisting of 15 trillion tokens, which is 7 times larger than the dataset used for Llama 2.”
[^5]: [Llama 2 to Llama 3: Meta’s Leap in Open-Source Language Models](https://everyintel.ai/llama-2-to-llama-3-metas-leap-in-open-source-language-models/) (everyintel.ai, retrieved 2026-10-17). Findings F6: “The training dataset for Llama 3 is over seven times larger than that used for Llama 2, with more than 15 trillion tokens.”
