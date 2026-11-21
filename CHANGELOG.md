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

The combat-loot build pins the Python image and NumPy. Local batch runs cap concurrency at two.
