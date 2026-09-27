# Synthetic assembly: branch and join

[assembly-branch.json](assembly-branch.json) is a discrete assembly input of the same kind as the reference factory. Names, products, orders, materials, operation times, calendars, resources, staff, rules and outcomes are all synthetic and do not describe a real manufacturer or plant. The fixed business start is 2026-09-19 08:00 (Asia/Singapore), and the configuration starts as DRAFT.

One order needs 20 wiring assemblies, in two batches of 10. Each piece consumes 1 shell and 2 contacts; opening stock is 20 shells and 40 contacts, nothing is reserved and no receipts are expected. Kitting reserves both materials for the whole batch; shell assembly and contact assembly consume them when their production actually starts.

| Operation | Predecessors | Setup minutes | Cycle seconds per piece | Machine type / skill | Material consumed |
|---|---|---:|---:|---|---|
| Kitting | none | 1 | 6 | KIT_BENCH / KIT | none |
| Shell assembly | Kitting | 0 | 18 | SHELL_BENCH / SHELL | 1 shell per piece |
| Contact assembly | Kitting | 0 | 24 | CONTACT_BENCH / CONTACT | 2 contacts per piece |
| Join assembly | Shell assembly, Contact assembly | 1 | 12 | JOIN_BENCH / JOIN | none |
| Final test | Join assembly | 0 | 12 | TEST_BENCH / TEST | none |

Each machine type and skill has one machine and one worker; the shell and contact branches can run at the same time, and every operation occupies one qualified worker throughout. Shell assembly and the final test are quality gates with unknown (null) numeric thresholds; the engine produces results along the declared synthetic success path. All machines and workers share two shifts, 08:00–08:20 and 08:25–09:00, without overtime.

Each batch takes 14 minutes of production by the existing formula; physical changeovers come on top: 1 minute for the first, 1 minute within the same product and 2 minutes between products. The planning window and the hard deadline both end at 09:00 that day; the freeze window is 10 minutes and continuous revalidation is off. The operation array in the JSON is deliberately not in execution order, so the order must be read from the dependencies.

This input checks that the contracts, CP-SAT, the independent Checker and the simulated execution engine support a branching routing. `profile.source_digest` and `source.evidence_digest` are the SHA256 of this note; the snapshot also keeps its standard content hash.
