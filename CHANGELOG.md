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
| Pursue isolated low-health opponents behind cover | 6.54 | Set aside. The matched comparison won 6 seeds, tied 13, and lost 5. |
| Prefer a clear in-range attacker to a blocked target | 7.33 | Kept. Won 7 matched seeds, tied 15, and lost 2 against the 6.33 build. Official Docker check passed ([run](https://github.com/davidvarvas/mech-royale-bot-gpt/actions/runs/44845598957)). |
| Rank gun upgrades by sustained damage at current weapon stats | 7.42 | Kept. The first drum magazine beats the damage upgrade at default stats, and the damage upgrade beats a second drum magazine. The simulator does not apply upgrade effects. The combined build averaged 12.88 over 24 matched local free-for-alls, up from 12.50. Official Docker check passed ([run](https://github.com/davidvarvas/mech-royale-bot-gpt/actions/runs/44845763942)). |

The combat-loot build pins the Python image and NumPy. Local batch runs cap concurrency at two.

Official practice scored **3.79** for `0003b8766127` and **4.08** for `89ec1cdb43ae`, each over 48 matches. The 4.08 run is the current official comparison point.
