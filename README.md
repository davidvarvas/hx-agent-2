# survivor — Mech Royale bot

A mechfight/1 bot that plays for placement:

- **Zone first:** rotates into the next circle before it shrinks; never steps across the closing edge.
- **Fights facing the enemy** (rear hits do 1.5x), strafing with lead-aimed gun fire; laser is charged and released on target; lightning fires when someone is in the cone; mortar lobs over cover.
- **Outnumbered:** moves to the nearby spot the fewest enemies can see while keeping its target in view; when losing, breaks line of sight to regenerate.
- **Between fights:** takes cubes, weapons (lightning > laser > mortar, keeps the gun), crates, NPC drones and supply drops.
- **Upgrades:** plating, armor, repair nanites first.

Files: `bot.py` (decisions), `nav.py` (2 m grid of the map: line of sight with numpy, A* on a 4 m grid), `mechfight.py` (stdin/stdout loop).
Answers in ~1 ms typically, under 10 ms worst case.
