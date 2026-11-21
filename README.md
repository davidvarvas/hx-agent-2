# Survivor

Survivor is a Mech Royale bot for the Builders Buildathon. It uses the live `mechfight/1` rules, weapon stats, and the official arena supplied in each match.

The bot rotates inside the shrinking zone, uses buildings to limit the number of opponents with a clear shot, and picks up nearby loot while keeping aim on a duel. Gun aim accounts for target motion, the firing tick, and turret turn speed. It supports the gun, laser, lightning, and mortar. It can hold a viable repair drop under fire and brakes toward the center of the final circle.

The Docker image pins the Python base image and uses NumPy 2.1.3 for map geometry. The code is MIT-licensed.

## Check the bot

Keep the official `mechfight-kit` folder beside this checkout. Build from this repository and run the evaluator from the kit directory.

```sh
docker build --platform linux/amd64 -t my-bot .
cd ../mechfight-kit
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" my-bot
```

The approximate practice simulator needs NumPy. From the repository root, run `NOISE=0.6 J=2 SEED0=200 python3 tools/batch.py 24 -- python3 -u bot.py`. See [CHANGELOG.md](CHANGELOG.md) for measured comparisons and official Docker checks.
