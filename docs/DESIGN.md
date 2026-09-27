# Design and interaction

The design rules of the web workbench. Product scope is in the [user stories](USER_STORIES.md).

## Users and information architecture

- The manager goes straight to `/agent/chat` and the disruption simulator to `/factory/facts`; there is no identity picker, and the two sides keep separate sessions and CSRF tokens.
- The manager side is a persistent frame: a sidebar (brand, new conversation, history grouped by "Today" and "Earlier", and at the bottom the production timeline, execution log and settings), a top bar (factory clock, plan state, orders due today, sync state and "Field changes"), and a main area that only switches views. Views change within the page. The sidebar can be hidden completely from the button next to the brand, leaving only a show button at the top left; the choice is remembered in the browser.
- A new tab opens a new conversation; a refresh keeps the conversation selected in that tab. A new conversation only shows a greeting with "Plan today", "Review the shop floor" and "Report a disruption", and never inserts cards by itself. Shop floor changes start a new conversation when the manager opens them from "Field changes".
- Production timeline: title, factory time and date on one line; below, "View plan" on the left (plans listed by conversation name, with "Back to its conversation" for previews) and on the right today's summary, in-progress and due-date-risk pills, the dispatch sheet export and "Display settings" (a centered dialog for merging operations, time axis width, full day and legend). Grouping is switched in the top-left corner of the chart, and the chart sits as high as possible.
- The execution log is organized by each manager approval: approval time, option, measures, new cash outlay, factory receipt and production progress. The timeline exports today's dispatch sheet as CSV.
- The simulator uses a few secondary colors for hierarchy: the run bar and left rail are a light stone tone, group titles carry a short clay-colored bar, table headers are light stone, disruption states use danger-colored tags, and dialog primary buttons are ink black. Dropdowns share the input style with a drawn arrow.
- The simulator is a single page with four groups: Orders and delivery, Inventory and supply, Machines and staff, Execution and quality. Disruptions start only from object rows in these groups, with no duplicate entry points; the left rail shows each group's open problems and "Disruptions this run". The top run bar only holds Run / Pause, speed, Advance and "Reset today"; random breakdowns and replay live under "More". The clock stays still until the manager approves the day's first plan. Records are edited in a centered dialog, one object at a time.
- Model choice and recommendation preference live in the settings dialog; a preference change needs confirmation, only changes the recommendation order and never relaxes constraints.

## Business meaning that must not be lost

An operation completed is not a qualified batch; a batch moved to stock is not available finished goods; an expected receipt is not stock on hand; a quote is not a purchase; an approved option is not an applied measure; a comparison result is not a formal plan; approval is not factory acceptance, and acceptance is not actual completion; production completed is not delivered.

Business dates are shown in the factory time zone as "09-26 08:45"; UTC and raw ISO text never appear in the interface. A search that ran out of time reads "No feasible schedule found within the time limit" and stays distinct from "Cannot be scheduled under current conditions"; feasible does not mean proven optimal.

## Conversation

| Message type | Presentation |
|---|---|
| Manager message | Ink-black bubble on the right with cream text |
| Agent reply | Plain text without a bubble, with a small "● Agent" label above |
| Tool activity | A single line with a thin left rule: action name · status from the real task result, with the Agent's reason below |
| Current activity | One line only while running: stage and elapsed seconds, with a stop button when stopping is possible |
| System notice | Light paper panel labelled "System", for calculation limits, execution in progress and similar holds |
| Plan card | White card that never truncates text |
| Response options | The Agent's short recommendation first, then numbered options (Option 1, 2, 3; the recommended one has an orange left border), each with its own "Comment" and "Approve and execute"; options not adopted are collapsed at the end |
| Execution receipt | Result card stating what is done and which steps can continue |
| Error | Light danger background with the cause and the next step |

- Cards belong only to the conversation that produced them; switching conversations shows a skeleton, never a flash of the greeting or of another conversation's cards.
- New content follows when the reader is at the bottom, and sending a message always scrolls to the bottom; reading further up is never interrupted, and "Back to latest" appears instead.
- Agent and tool text is cleaned before display: internal field names, solver codes and ISO times become business wording.
- Plan card structure: type and state → measures (a pure reschedule says "No extra purchases or resource measures") → key figures → order impact → required confirmations → approve button and "Request changes" → source note (Preset plan · Checked, or Checked · Not proven optimal).
- Approval happens only on the card. The Agent refers to options by their card titles without repeating card figures in text; while a card awaits a decision, chat suggestion buttons never offer approve, submit or execute.
- Interface and Agent text use real factory wording, without "simulated" or "demo"; "Disruption simulator" is only the name of the tool that creates disruptions.
- The Agent reports like a production supervisor: conclusion and recommendation first, then the key figures, with assumptions stated as "on the current schedule" or "if nothing else changes on the shop floor", never as disclaimers. Questions about delivery and progress are answered from the facts the service computes, not by pasting a fixed report.
- Stopping an analysis does not pause the factory or withdraw an approved plan. When a network result is unknown, the same request is checked again instead of being resent under a new ID.

## Forms, lists and dialogs

- An edit dialog keeps the object and version it opened with; when the data changes in the background, the input is kept and loading the latest data is an explicit choice. Closing with unsaved changes asks whether to keep editing or discard.
- While a write awaits confirmation, repeated submission is blocked, and the same request can be recovered after closing or refreshing. Cancelling an order, deleting a quote or scrapping a whole batch shows the impact and needs explicit confirmation.
- Dialogs have a name, a focus trap, Escape to close and focus return; errors are tied to their fields, and state never depends on color alone. Settings-type content uses centered dialogs rather than dropdown panels that a chart could cover.

## Visual system

- `apps/web/src/tokens.css` is the only source of token values; `globals.css` is the last theme layer to load, and component styles only reference token names.
- Colors: warm paper canvas `#F3F0EE`, secondary panel `#FCFBFA`, card white `#FFFFFF`, ink `#141413`, secondary text `#696969`, hairline `rgba(20,20,19,.12)`.
- Signal orange `#CF4500` is reserved for "Approve and execute" and for the small label dots and the factory clock dot. Ordinary primary actions are ink-black pills, secondary actions are white with a hairline, and weak actions are transparent.
- Semantic colors come in pairs: success `#2F6B3A`/`#EAF1EA`, warning `#8A5A00`/`#F6EEDB`, error `#B42318`/`#F8E7E4`, information `#3860BE`/`#E9EEF9`.
- Radii: 6px tags; 12px inputs and small cards; 20px buttons, messages and cards; 28px the composer and dialogs; 999px the factory clock, state pills and icon buttons.
- Type: Sofia Sans for Latin text and figures (body weight 450) with a system fallback; headings weight 500 with -0.02em tracking; tabular figures.
- Shadows: none by default; raised `0 4px 24px rgba(0,0,0,.04)`; overlays `0 24px 48px rgba(0,0,0,.08)`. Motion is limited to 150ms fades and expansions and respects reduced-motion settings.
- The identity comes from four fixed elements: the warm paper canvas, ink-black pill primary buttons, signal orange reserved for approval, and the ink-black factory clock shared by both sides. No round avatars, orbit arcs, watermark headlines or dark footers.
- No marketing copy, tutorials or repeated step descriptions in the interface; keep business facts, times, costs, risks, errors and the confirmations that are needed.

## Checks for interface changes

Component interaction, contract, lint, type and build checks must pass. Changes are also reviewed in a real browser on a projector-sized screen, a laptop and a narrow screen, with long text and long tables, keyboard use, scrolling, focus, overlay stacking, and error, loading and empty states. Automated tests do not replace this visual review, and interface work never changes business facts, permissions, idempotency or approval boundaries.
