# BallsDex V3 Boss Package

Boss battle system for **BallsDex V3**. Players battle against powerful boss enemies using their countryballs.

## Commands

| Command | Description |
|---|---|
| `/boss admin_start` | Start a boss battle with the specified ball and HP. Choose a countryball to be the boss (required). Choose HP (Required) |
| `/boss select` | Select countryball to use against the boss. Players choose an item to use against the boss using this command |
| `/boss admin_attack` | Start a round where the Boss Attacks. With this command you can choose how much attack the boss deals (Optional, Defaulted to RNG from default 0 to 2000, can be changed in code) |
| `/boss admin_defend` | Start a round where the Boss Defends |
| `/boss admin_end_round` | End the current round and displays user performance about the round |
| `/boss admin_conclude` | Finish the boss, conclude the Winner. This ends the boss battle and rewards the winner, but you can choose to have *no* winner |
| `/boss ongoing` | Show your damage to the boss in the current fight |
| `/boss admin_disqualify` | Disqualify a member from the boss |
| `/boss admin_hackjoin` | Force join a user to the boss battle |
| `/boss admin_ping` | Ping all the alive players |
| `/boss admin_stats` | See current stats of the boss |

**How to Play:** Some commands can only be used by admins, these control the boss actions. Players can join using the join button that appears when the boss starts. Repeat the attack/defend rounds until the boss' HP runs out, then conclude the battle.

## Installation

### 1 — Important Notes

1. **You must have a special called "Boss" in your dex** - This is for rewarding the winner. Make it so the special's end date is 1970. Rarity must be 0.

2. **Only use a countryball as a boss** if it has both the collectible and wild cards stored, otherwise this will result in an error. Cards without wild cards do not work as a boss. If you are using a ball made from the admin panel for the boss, then it's fine, since admin panel requires wild card.

3. You may change the shiny buffs in the code to suit your dex better - it's defaulted at 1000 HP & 1000 ATK.

### 2 — Configure extra.toml

**If the file doesn't exist:** Create a new file `extra.toml` in your `config` folder under the BallsDex directory.

**If you already have other packages installed:** Simply add the following configuration to your existing `extra.toml` file. Each package is defined by a `[[ballsdex.packages]]` section, so you can have multiple packages installed.

Add the following configuration:

```toml
[[ballsdex.packages]]
location = "git+https://github.com/MoOfficial0000/BossPackageBD.git"
path = "boss"
enabled = true
```

**Example of multiple packages:**

```toml
# First package
[[ballsdex.packages]]
location = "git+https://github.com/example/other-package.git"
path = "other"
enabled = true

# Boss Package
[[ballsdex.packages]]
location = "git+https://github.com/MoOfficial0000/BossPackageBD.git"
path = "boss"
enabled = true
```

### 3 — Rebuild and start the bot

```bash
docker compose build
docker compose up -d
```

This will install the package and start the bot.
