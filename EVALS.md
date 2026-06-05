# Eval cases — what good output looks like

Concrete reference outputs for each of the four LLM surfaces in the project, with annotations explaining what makes each one "good." These aren't unit tests in the deterministic sense — every call is stochastic (high temperature for creativity) — but they describe the *shape, voice, and grounding* every call should consistently produce.

The four surfaces this project routes between:

| Surface | Model | Role |
| --- | --- | --- |
| Scenario gen + auto-action + suggestions | `gpt-4o-mini` | Fast, structured-JSON inventions |
| Rule Enforcer agent | `gpt-4o-mini` (tool-use loop) | Multi-turn autonomous tool calling |
| Narrator | `claude-sonnet-4-6` (default, swappable) | Vivid in-genre prose |
| Companion Q&A | `gpt-4o-mini` | In-character helper grounded in current state |

Each eval below shows the input, a representative good output, and the success criteria.

---

## 1. Scenario generation

**Input:**
```bash
curl -X POST http://127.0.0.1:8000/api/scenario \
  -H "Content-Type: application/json" \
  -d '{"difficulty": "easy"}'
```

**Representative good output:**
```json
{
  "genre": "salt-flat necromancy",
  "location": "A sunbaked watchtower at the lip of the great salt flats, where bone-pale flags snap in the heat-wind.",
  "objective": "Recover the moonglass amulet from the shattered telescope at the tower's crown.",
  "starting_items": [
    "a tarnished brass spyglass",
    "a flask of mirror-dew",
    "a hand-stitched journal of fading stars"
  ],
  "starting_enemies": [
    {
      "name": "Husk-Watcher",
      "hp": 10,
      "location": "A sunbaked watchtower at the lip of the great salt flats, where bone-pale flags snap in the heat-wind.",
      "threat": "low",
      "description": "A robed figure stripped to dry bone by the sun, still raising a brass telescope as if watching for something that never came."
    }
  ],
  "companion": {
    "name": "Halid the Cartographer",
    "avatar": "🧭",
    "persona": "A weather-creased mapmaker who has crossed the salt flats nine times and never the same way twice; speaks in latitude and superstition.",
    "intro": "Welcome, traveler. This watchtower has been dead since the second sun set on the flats — but the moonglass amulet still hums up there in the broken telescope, I can feel it from here. I'll point you true; the flats and I have an arrangement. Mostly."
  },
  "difficulty": "easy",
  "max_turns": 10
}
```

**What makes it good:**
- **Specific and in-genre:** "salt-flat necromancy" is evocative; the spyglass / moonglass / watchtower thread is coherent across location, items, and objective.
- **Easy-mode scope respected:** the objective sits in the same tower as the starting location — reachable in 3–5 turns of focused play (matches `DIFFICULTY_PROFILES["easy"].scope`).
- **Enemy count appropriate:** exactly 1 weak enemy (HP 10, threat "low"). Easy mode is `(0, 1)` enemies with `hp_range=(6, 14)`.
- **Companion is concrete:** weather-creased mapmaker with a quirk (superstition + latitude), not a generic "friendly guide." The intro hints at reliability ("I'll point you true … Mostly") which matches the easy-mode reliability score 9/10.
- **Shape is right:** all required fields, items are exactly 3, `max_turns: 10` matches the difficulty profile.

---

## 2. Rule Enforcer agent — combat turn

**Input** (the turn endpoint, internally):
```
Player action: "I swing my torch at the wraith"
State: HP 88, turn 3 / 10, location "Crypt Antechamber"
Enemies: [{id: "e1", name: "Shadow Wraith", hp: 18, location: "Crypt Antechamber", threat: "medium"}]
Difficulty: "normal"
```

**Representative good agent loop** (visible in the MCP Inspector's Recent Calls + the per-turn telemetry):
```
Iteration 1 (agent → tool call):
  damage_enemy({"enemy_id": "e1", "damage": 9})
  → wraith hp drops to 9, still alive

Iteration 2 (agent → tool call):
  mutate_world_state({"health_change": -7})
  → player hp drops to 81, turn_count advances to 4

Iteration 3 (agent → final text, no tool):
  "The wraith took a torch blow to its smoke-flank but countered
   with a chill lash; both combatants now wounded and exchanging
   ground in the antechamber."
```

**What makes it good:**
- **Damage is in-profile:** normal-difficulty enemy damage range is 5–15; the agent applied 7 against the player and 9 against the enemy (plausible torch swing).
- **Single mutate_world_state call** — exactly one call commits the player-state outcome, so the turn counter advances by 1 (not 2). The orchestrator guards against double-mutate at the framework level, but the agent is also taught to call once.
- **Right tool for right effect:** player damage flows through `mutate_world_state(health_change=-7)`, NOT `damage_enemy`. The agent doesn't confuse the two surfaces (a common LLM failure mode caught by the system prompt's "DAMAGE RULES" block).
- **No phantom enemy spawn:** the wraith already existed at the player's location; the agent does NOT call `add_enemy` for it again (the prompt now hard-bans duplicates).
- **Final iteration is plain text** explaining the ruling — no further tool call, so the loop exits cleanly.

---

## 3. Companion Q&A

**Input:**
```bash
curl -X POST http://127.0.0.1:8000/api/companion/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What am I carrying again?",
    "history": []
  }'
```

With current state:
```
inventory: ["brass spyglass", "vial of moonglass", "torch of cold-fire"]
companion: {name: "Halid the Cartographer", persona: "weather-creased mapmaker..."}
difficulty: "easy" → reliability 9/10 (Trustworthy)
```

**Representative good output:**
> *"You're carrying a brass spyglass — keep that one close, the salt eats lenses like locusts. There's a vial of moonglass too, though I'd not crack it open until you're under stars. And that torch of cold-fire? Don't ask me where it came from; I learned a long time ago not to question gifts that burn blue."*

**What makes it good:**
- **Grounded in actual state:** names each item exactly as it appears in `state.inventory`. No hallucinated items.
- **Stays in character:** the mapmaker voice from the persona ("the salt eats lenses like locusts", "gifts that burn blue") — superstition + lived experience.
- **Adds value beyond the list:** light flavor commentary on each item that hints at usage without spoiling.
- **Doesn't break the fourth wall:** never says "you have 3 items" or mentions game mechanics.
- **Length-appropriate:** 3 sentences for a casual inventory question. Helpfulness matches easy-mode profile (warm, freely volunteers advice).

**Counter-example — bad companion output** (this is what we *don't* want):
> *"You have 3 items in your inventory: brass spyglass, vial of moonglass, torch of cold-fire. Would you like more details?"*

That's grounded in state but breaks character entirely. Generic, lists like a menu, mentions "inventory" as a system concept.

---

## 4. Narrator output with pill markers

**Input** (Narrator gets state-after-turn + mutation + events list):
```
Player action: "I leap across the chasm and grab for the relic"
Mutation: {health_change: -8, add_items: ["moonglass amulet"], current_location: "the broken crown of the watchtower", has_objective_item: true}
Events (in order):
  [PILL 0] 💔 Took damage — 8
  [PILL 1] 🧭 Moved — the broken crown of the watchtower
  [PILL 2] 🎒 Found — moonglass amulet
  [PILL 3] 🎯 Objective acquired!
```

**Representative good output:**
> You hurl yourself across the gap, the salt-wind tearing at your coat. Stone bites your ribs as you slam into the far edge, hands scrabbling for purchase. [[PILL 0]]
>
> You haul yourself up onto the broken crown of the watchtower, blinking grit from your eyes — and there it is, the moonglass amulet, still hanging from the shattered telescope's brass eye. [[PILL 1]]
>
> Your fingers close around it. The amulet is colder than the stone, colder than the wind. [[PILL 2]] [[PILL 3]] Far below, the salt flats begin to whisper, as if they too have noticed.

**What makes it good:**
- **In-genre, atmospheric prose** — sensory detail ("salt-wind", "stone bites your ribs", "colder than the wind") matches the established salt-flat necromancy tone.
- **Doesn't list mechanics** — no "you take 8 damage", no "your HP is now 80". Damage is conveyed through *"stone bites your ribs"*.
- **Markers placed correctly** — each `[[PILL N]]` lands at the end of the sentence that narratively describes that event. PILL 0 (damage) lands right after the impact sentence. PILL 1 (move) right after the haul-up sentence. PILL 2 + PILL 3 (item gain + objective) cluster naturally on the same line because acquiring the item *is* completing the objective.
- **Ends on a sensory beat** — "the salt flats begin to whisper, as if they too have noticed" sets up the next action without dictating it.
- **Length** — 3 short paragraphs, in the 2–4 paragraph guideline.
- **No fourth-wall breaks** — no "turn", "objective", "HP" terms in the prose.

---

## Where to see these live

Spin up the app, open the **🔌 MCP Inspector** pane (right side, click the rail), and watch the per-turn telemetry + Recent Calls list. Every category above is observable as it happens:

- Tool calls with their args + roundtrip ms → "Recent Calls"
- The agent's reasoning per iteration → "Live Telemetry"
- The narrator's pill markers being placed → the inline pills in the narration pane
- Companion responses → the left companion pane (when expanded)
