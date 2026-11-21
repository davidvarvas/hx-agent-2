# Mech Royale experiments

Local practice tests use the kit map, 24 matched seeds (200–223), `NOISE=0.6`, and at most two simultaneous matches. Scores come from the approximate simulator. The Docker check replays 300 official observations under the match limits.

| Change | Mean points | Result |
| --- | ---: | --- |
| Starter archive | 4.71 | Official Docker check passed ([run](https://github.com/davidvarvas/mech-royale-bot-gpt/actions/runs/44842271430)). |
| Earlier retreat and cover-only healing | 4.33 | Dropped. Fewer kills despite longer survival. |
| Collect nearby cubes and weapons while dueling | 5.08 | Kept. Finished with a second weapon in 16 matches, up from 12. Official Docker check passed ([run](https://github.com/davidvarvas/mech-royale-bot-gpt/actions/runs/44842661062)). |
| Predict intercepts and turret aim for the firing tick | 5.96 | Kept. No late actions in the 24-match batch. Official Docker check passed ([run](https://github.com/davidvarvas/mech-royale-bot-gpt/actions/runs/44842802350)). |
| Read weapon, movement, and hitbox values from live rules | 5.96 | Kept. Same score with the kit balance. Official Docker check passed ([run](https://github.com/davidvarvas/mech-royale-bot-gpt/actions/runs/44843004781)). |
| Target direct threats and favor advantageous fights | 6.92 | Kept. A separate 24-seed free-for-all model improved from 12.00 to 12.58. Official Docker check passed ([run](https://github.com/davidvarvas/mech-royale-bot-gpt/actions/runs/44843441253)). |
| Bound pathfinding work per decision | 6.67 | Kept for timing. The 24-match run had zero late actions, down from four, and a 25 ms observed maximum on the shared Mac. Official Docker check passed ([run](https://github.com/davidvarvas/mech-royale-bot-gpt/actions/runs/44843749860)). |
| Approach and hold the exact final center | 6.21 | Kept for the final. The practice simulations ended before the last shrink. In three open-ground tests using the kit's acceleration and decision rate, the controller settled within one micrometer of center. Official Docker check passed ([run](https://github.com/davidvarvas/mech-royale-bot-gpt/actions/runs/44843893058)). |
| Predict and dodge incoming gun rounds | 6.29 | Dropped. Eight late actions on the shared Mac and little scoring change. |
| Prefer laser over lightning | 6.25 | Dropped. The local comparison showed little benefit. |
| Score incoming and outgoing cover lanes separately | 5.83 | Dropped. The cover search cost kills in the local comparison. |
| Pursue viable repair drops during combat | 6.33 | Kept. The simulator has no drops. Seven targeted checks covered approach, capture, nearby enemies, landing time, and zone safety. The same 24 local free-for-all seeds averaged 12.50, compared with 12.58 for the scored build. Official Docker check passed ([run](https://github.com/davidvarvas/mech-royale-bot-gpt/actions/runs/44844518493)). |

The combat-loot build pins the Python image and NumPy. Local batch runs cap concurrency at two.

Official practice scored **3.79** over 48 matches for commit `0003b8766127`. It is the comparison point for later official runs.
