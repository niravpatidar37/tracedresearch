# Partial findings

**Question:** What are the main differences between the Raft and Paxos consensus algorithms in practice?

## Summary

Writer not run \(writer output invalid\); listing extracted findings per sub-question. [^1]

## What are the key components and design principles of Raft?

Raft offers a generic way to distribute a state machine across a cluster of computing systems, ensuring that each node in the cluster agrees upon the same series of state transitions. [^1]

Raft divides the consensus process into clear components such as leader election, log replication, and safety, making the algorithm simpler to reason about. [^2]

## What are the key components and design principles of Paxos?

Paxos guarantees safety \(consistency\), and the conditions that could prevent it from making progress are difficult to provoke. [^3]

PAXOS operates below the belief that the network is asynchronous. [^4]

Paxos deliberately sacrifices guaranteed liveness to keep absolute safety. [^5]

In general, a consensus algorithm can make progress using processors, despite the simultaneous failure of any processors. [^3]

## How do Raft and Paxos compare in terms of fault tolerance and performance in real-world applications?

Raft was designed to be more understandable than Paxos while providing equivalent fault tolerance guarantees. [^6]

Paxos has been proven correct and widely implemented in production systems. [^6]

Raft is more understandable and can be easily implemented compared to Paxos. [^7]

Raft uses a strong leader approach where one node acts as the leader, handling all client requests and log replication. [^6]

## Limitations

- Report generated without the writer model because of: writer output invalid.

## Sources

[^1]: [Raft \(algorithm\) - Wikipedia](https://en.wikipedia.org/wiki/Raft_%28algorithm%29) (en.wikipedia.org, retrieved 2026-10-17). Findings F1: “Raft offers a generic way to distribute a state machine across a cluster of computing systems, ensuring that each node in the cluster agrees upon the same series of state transitions.”
[^2]: [Raft Consensus Algorithm - GeeksforGeeks](https://www.geeksforgeeks.org/system-design/raft-consensus-algorithm/) (geeksforgeeks.org, retrieved 2026-10-17). Findings F2: “It divides the consensus process into clear components such as leader election, log replication, and safety, making the algorithm simpler to reason about.”
[^3]: [Paxos \(computer science\) - Wikipedia](https://en.wikipedia.org/wiki/Paxos_%28computer_science%29) (en.wikipedia.org, retrieved 2026-10-17). Findings F3, F6: “Although no deterministic fault-tolerant consensus protocol can guarantee progress in an asynchronous network \(a result proved in a paper by Fischer, Lynch and Paterson\[6\]\), Paxos guarantees safety \(consistency\), and the conditions that cou”
[^4]: [PAXOS Consensus Algorithm - GeeksforGeeks](https://www.geeksforgeeks.org/computer-networks/paxos-consensus-algorithm/) (geeksforgeeks.org, retrieved 2026-10-17). Findings F4: “Asynchronous Network: PAXOS operates below the belief that the network is asynchronous. In this approach messages among nodes can be delivered in any order, or maybe not at all.”
[^5]: [Paxos Consensus Algorithm Explained Simply · Penluma](https://penluma.com/blog/distributed-systems/06-paxos-the-original-consensus-algorithm/) (penluma.com, retrieved 2026-10-17). Findings F5: “This is not a bug. It is the FLP impossibility result showing up in real life: in a fully asynchronous system, with no bound on message delays and no perfect way to detect failures, no consensus algorithm can guarantee it always terminates.”
[^6]: [Distributed Consensus: Raft and Paxos Algorithms Explained with Examples - CodeLucky](https://codelucky.com/distributed-consensus-raft-paxos/) (codelucky.com, retrieved 2026-10-17). Findings F7, F8, F10: “Raft, introduced by Diego Ongaro and John Ousterhout in 2014, was designed to be more understandable than Paxos while providing equivalent fault tolerance guarantees.”
[^7]: [Which is Better for production: Paxos or Raft?](https://thescalabilitydigest.substack.com/p/which-is-better-for-production-paxos) (thescalabilitydigest.substack.com, retrieved 2026-10-17). Findings F9: “Paxos is considered for its theoretical elegance, but it is also criticised for being notoriously difficult to understand and implement. On the other hand, Raft is much more understandable and can be easily implemented and thus can be more”
