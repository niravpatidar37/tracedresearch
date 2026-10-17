# Research report

**Question:** What changed technically between Llama 2 and Llama 3?

## Summary

Llama 3 introduces several technical changes over Llama 2, notably a new attention mechanism and larger tokenizer, while also expanding context windows for its models. [^1]

## Architectural Differences

Llama 3 adopts Grouped Query Attention \(GQA\), a design that groups queries to reduce computational load during inference, a feature absent in Llama 2. [^1]

The tokenizer in Llama 3 is more efficient, supporting 128,000 tokens, whereas Llama 2’s tokenizer size is not specified in the available data. [^1]

## Parameter Count and Context Window

The 8 B Llama 3 model uses an 8,192‑token context window, double the 4,096‑token window of the 8 B Llama 2 model. [^2]

Similarly, the 70 B Llama 3 model also uses an 8,192‑token context window, whereas the 70 B Llama 2 model uses 4,096 tokens. [^2]

## Limitations

- No data on training dataset size changes for Llama 3 versus Llama 2.
- No data on reported inference latency or hardware efficiency improvements for Llama 3 versus Llama 2.
- Unanswered: What changes were made to the training dataset size and composition for Llama 3 compared to Llama 2? (search budget reached: research status: no\_results)
- Unanswered: What are the reported inference latency and hardware efficiency improvements of Llama 3 over Llama 2? (send-back limit reached: research status: no\_results)

## Sources

[^1]: [LLAMA 2 Vs LLAMA 3 - Blockchain Council](https://www.blockchain-council.org/ai/llama-2-vs-llama-3/) (blockchain-council.org, retrieved 2026-10-17). Findings F1, F2: “- Model Architecture: LLAMA 3 incorporates enhancements like Grouped Query Attention \(GQA\) to improve inference efficiency.”
[^2]: [Llama 3 vs Llama 2](https://www.apps4rent.com/blog/llama-3-vs-llama-2/) (apps4rent.com, retrieved 2026-10-17). Findings F3, F4: “\| Context Length \| 8192 tokens \| 4096 tokens \|”
