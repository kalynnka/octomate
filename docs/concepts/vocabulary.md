# Vocabulary

| Word | Meaning |
|---|---|
| Octomate | The coordinator: owns every tentacle and the managers they share |
| Tentacle | A lifecycle component the host owns; a channel, an agent, or a connector |
| Chromo | A channel's translator: platform payloads in, core schemas out, and back |
| Ink | A channel's transport: what actually sends, edits, uploads and opens DMs |
| Feeler | A channel's view: timeline, markdown, segments, approvals, questions, OAuth |
| Siphon | A channel's way out: draws what the nerve carries to it through its feelers, each thread in order |
| Awake | Input that starts a reflex or answers a live deferred request |
| Asleep | A signal asking an active run to stop gracefully |
| Kick | A request to wake an agent through the signal nerve |
| Nerve | A one-way stream: signals go to agents; events report what they do |
| Impulse | One outbound event, with the address it happens at |
| Sensing | An agent tentacle receiving signals, applying controls and starting reflexes |
| Reflex | The graph executed by the receiving agent tentacle, and the package holding it |
| Claim | What a route advertises: an ability and the efforts it accepts |
| Route | An `(agent, model)` pair a channel can reach, with its claim; also the graph node that picks one |
| Spell | A gateway tool: inspect, summon, teleport, scheme, send, dismiss |
| Spill | An oversized tool return held out of the context until the model asks |
| Segment | One piece of a message: text, markdown, mention, image, file, card, reply |
| Surface | The place a thread is on: itself, or the chat room it was opened in |

"Triage" survives in a module name and some comments as an older name for what is
now the reflex graph; there is no triage step.
