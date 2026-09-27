# Reference scenario

The bundled demo factory models the final assembly of four sealed single-row deep groove ball bearings, 6202-2RS1 to 6205-2RS1. Product dimensions, bearing structure and general manufacturing and quality practice follow the public SKF information listed below. The plant layout, operation times, stock, orders, calendars, prices and operating rules were designed by the team for this project: they are synthetic data, not SKF data and not the records of any real plant. SKF is a trademark of its owner; this project is not affiliated with or endorsed by SKF.

![Data and model provenance: public references, the team's planning abstraction and the synthetic operating scenario become a validated factory snapshot; CP-SAT, the independent Checker and business economics produce the option metrics; the language model only explains](assets/data_provenance.jpg)

Every number on a response option comes from the snapshot, the solver, the Checker and the versioned price catalog. Links to the language model are logical information dependencies through the runtime, not direct database access; model text can be wrong, and approval and execution use the structured record.

## The factory

**Products.** Four bearings built in batches of 50:

| Product | Bore d | Outside diameter D | Width B | Catalog weight |
|---|---:|---:|---:|---:|
| 6202-2RS1 | 15 mm | 35 mm | 11 mm | 0.0454 kg |
| 6203-2RS1 | 17 mm | 40 mm | 12 mm | 0.0656 kg |
| 6204-2RS1 | 20 mm | 47 mm | 14 mm | 0.1062 kg |
| 6205-2RS1 | 25 mm | 52 mm | 15 mm | 0.1303 kg |

**Bill of materials.** Each piece uses an inner ring, an outer ring, a ball set and a cage of its size, two RS1 contact seals, one grease fill unit and one box: 25 materials in total. Kitting reserves the material of a whole batch; each component is consumed when the operation that uses it starts.

**Routing.** Eight operations per batch, in this dependency order:

| Code | Operation | Minutes per batch of 50 | Resource | Skill |
|---|---|---:|---|---|
| OP10 | Kitting and pre-assembly prep | 10 | KIT-01 | Pre-assembly |
| OP20 | Ring and rolling element assembly | 9 | ASMCELL-A or ASMCELL-B | Assembly |
| OP30 | Cage assembly | 6 | ASMCELL-A or ASMCELL-B | Assembly |
| OP60 | Pre-lubrication and sealing check | 9 | TEST-01 | Inspection |
| OP40 | Grease filling | 6 | LUBE-01 | Lubrication and sealing |
| OP50 | Seal installation | 8 | SEAL-01 | Lubrication and sealing |
| OP70 | Final inspection and release | 9 | FINS-01 | Inspection |
| OP80 | Marking and packaging | 9 | PACK-01 | Packaging |

Changeovers take 1 minute between batches of the same product and 5 minutes between products. Eight workers (W01–W08) cover the five skills; one qualified worker runs an operation from start to end.

**Orders and calendar.** Six confirmed orders for 5,400 pieces in total (108 batches, 864 operations) are due over a five-day window. Each day has two regular shifts (08:30–12:00 and 13:00–17:30, Asia/Singapore) and a possible overtime window from 17:30 to 19:30 that needs the manager's approval. Work starting within the next 60 minutes is frozen. In the running demo, **Reset today** moves the same scenario to the current day.

**Two factories.** `skf-reference` keeps the baseline exactly as defined and is never changed by the demo. `skf-workshop` is the factory the demo works on: the same orders and constraints, plus a small buffer of 6204 components so that a rush order can be shown.

**Economics.** Prices, supply lead times, repair and cover costs come from a fixed, versioned catalog in SGD ([economics.py](../packages/domain/economics.py)). They are assumptions for comparing options, not supplier quotes.

The complete tables are in [database/](../database/README.md); their SHA256 digests are pinned, and the application refuses a changed baseline.

## Public sources

The following public pages and documents informed the product data and the shape of the routing. Links were checked in September 2026.

**Product pages** (dimensions, catalog weight, single-row design, bearing steel, normal radial internal clearance):

- [6202-2RS1](https://www.emarketplace.in.skf.com/deep-groove-ball-bearing/6202-2rs1), [6203-2RS1](https://www.emarketplace.in.skf.com/deep-groove-ball-bearing/6203-2rs1), [6204-2RS1](https://www.emarketplace.in.skf.com/deep-groove-ball-bearing/6204-2rs1), [6205-2RS1](https://www.emarketplace.in.skf.com/deep-groove-ball-bearing/6205-2rs1) — SKF India E-Marketplace.

**Bearing structure and lubrication**:

- [Rolling bearings](https://cdn.skfmediahub.skf.com/api/public/0901d196802809de/pdf_preview_medium/0901d196802809de_pdf_preview_medium.pdf) (SKF catalog): components and materials (inner ring, outer ring, rolling elements, cage, seals); sealed bearings are greased at the factory; RS1 contact seals and the standard stamped steel cage.
- [Deep groove ball bearings — interactive course transcript](https://www.skf.com/skf/campaign/aptitudearchive/training/dgbb_v2/presentation_content/external_files/DGBB_Transcript.pdf) (SKF): the 2RS1 designation, sealing and greased-for-life bearings, cages.
- [SKF bearings and mounted products, publication 100-700](https://www.skf.com/binaries/pub12/Images/0901d196807026e8-100-700_SKF_bearings_and_mounted_products_2018_tcm_12-314117.pdf): Conrad assembly, raceway grinding, the standard steel cage, and grease filling of sealed bearings (typically 25–35% of the free internal space).
- [Insert bearings and ball bearing units, PUB BU/P2 18033 EN](https://www.skf.com/search?search=18033) (SKF): a related product family whose assembly overview lists ball filling, cage fitting, inspection, grease filling and sealing, marking and packaging.

**Manufacturing and quality practice**:

- [How SKF manufacture bearing performance and quality](https://evolution.promo.skf.com/acton/media/22087/how-skf-manufacture-bearing-performance-and-quality): an SKF Manufacturing Academy session on deep groove ball bearing manufacture.
- [An efficient approach to high performance component production](https://evolution.skf.com/en/an-efficient-approach-to-high-performance-component-production-2/) (SKF Evolution): ring manufacturing routes before assembly.
- [The sound of noise](https://evolution.skf.com/us/sound-of-noise/) (SKF Evolution): vibration checks and cleanliness on bearing production lines.
- [Nondestructive testing at SKF](https://evolution.skf.com/us/nondestructive-testing-at-skf/) (SKF Evolution): ultrasonic and eddy current inspection in bearing manufacture.
- [New app for tracing bearings](https://evolution.skf.com/us/new-app-for-tracing-bearings/) (SKF Evolution): traceability marking that links manufacturing and measurement data.

These sources describe products and general practice. None of them states the operation times, ball counts, grease quantities, stock levels, orders or shift patterns used here, and the scenario should not be read as a description of any SKF factory.
