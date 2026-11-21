# Mech Royale experiments

Local practice tests use the kit map, 24 matched seeds (200–223), `NOISE=0.6`, and at most two simultaneous matches. Scores come from the approximate simulator. The Docker check replays 300 official observations under the match limits.

| Change | Mean points | Result |
| --- | ---: | --- |
| Starter archive | 4.71 | Official Docker check passed ([run](https://github.com/davidvarvas/mech-royale-bot-gpt/actions/runs/44842271430)). |
| Earlier retreat and cover-only healing | 4.33 | Dropped. Fewer kills despite longer survival. |
| Collect nearby cubes and weapons while dueling | 5.08 | Kept. Finished with a second weapon in 16 matches, up from 12. Official Docker check passed ([run](https://github.com/davidvarvas/mech-royale-bot-gpt/actions/runs/44842661062)). |
| Predict intercepts and turret aim for the firing tick | 5.96 | Kept. No late actions in the 24-match batch. Official Docker check passed ([run](https://github.com/davidvarvas/mech-royale-bot-gpt/actions/runs/44842802350)). |

The combat-loot build pins the Python image and NumPy. Local batch runs cap concurrency at two.
