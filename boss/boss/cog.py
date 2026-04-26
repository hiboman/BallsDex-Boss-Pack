from __future__ import annotations

import logging
import random
import string
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from django.db import models
from django.utils import timezone

from bd_models.models import Ball, BallInstance, Player
from bd_models.models import balls as balls_cache
from bd_models.models import specials
from ballsdex.core.utils.transformers import BallTransform, BallInstanceTransform
from ballsdex.core.utils import checks
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.boss")
Interaction = discord.Interaction["BallsDexBot"]

# IMPORTANT NOTES, READ BEFORE USING
# 1. YOU MUST HAVE A SPECIAL CALLED "Boss" IN YOUR DEX, THIS IS FOR REWARDING THE WINNER.
#    MAKE IT SO THE SPECIAL'S END DATE IS 1970. RARITY MUST BE 0
# 2. ONLY USE A COUNTRYBALL AS A BOSS in /boss start IF IT HAS BOTH THE COLLECTIBLE AND WILD CARDS STORED,
#    OTHERWISE THIS WILL RESULT TO AN ERROR.
#    there's a chance you may have not selected a wild card as it isn't required.
#    Cards without wild cards do not work as a boss, as again, this will result in an error.
#    If you are using a ball made from the admin panel for the boss, then it's fine, since admin panel requires wild card.
# 3. You may change the shiny buffs below to suit your dex better it's defaulted at 1000 HP & 1000 ATK
# 4. Please report all bugs to user @moofficial on discord
# 5. Finally, make sure to add the following to your config/extra.toml file:
#    [[ballsdex.packages]]
#    location = "git+https://github.com/MoOfficial0000/BossPackageBD.git"
#    path = "boss"
#    enabled = true
#    editable = false

# HOW TO PLAY
# Some commands can only be used by admins, these control the boss actions.
# 1. Start the boss using /boss admin start command. (ADMINS ONLY)
#    Choose a countryball to be the boss (required). Choose HP (Required)
# 2. Players can join using /boss join command.
# 3. Start a round using /boss admin defend or /boss admin attack.(ADMINS ONLY)
#    With /boss admin attack you can choose how much attack the boss deals (Optional, Defaulted to RNG from default 0 to 2000, can be changed below)
# 4. Players now choose an item to use against the boss using /boss select
# 5. /boss admin end_round ends the current round and displays user permformance about the round (ADMIN ONLY)
# 6. Step 3-5 is repeated until the boss' HP runs out, but you can end early with Step 7.
# 7. /boss admin conclude ends the boss battle and rewards the winner, but you can choose to have *no* winner (ADMIN ONLY)

# Configuration constants
SHINY_BUFFS = [1000,1000] # Shiny Buffs
# ATK, HP
MAX_STATS = [5000,5000] # Max stats a card is limited to (before buffs)
# ATK, HP
DAMAGE_RANGE = [0,2000] # Damage a boss can deal IF attack_amount has NOT been inputted in /boss admin attack.
# Min Damage, Max Damage

class JoinButton(discord.ui.View):
    """Join button for boss battles"""
    def __init__(self, boss_cog):
        super().__init__(timeout=900)  # 15 minutes timeout
        self.boss_cog = boss_cog
        self.join_button = discord.ui.Button(
            label="Join Boss Fight!", 
            style=discord.ButtonStyle.primary, 
            custom_id="join_boss"
        )
        self.join_button.callback = self.button_callback
        self.add_item(self.join_button)

    async def button_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if not self.boss_cog.boss_enabled:
            return await interaction.followup.send("Boss is disabled", ephemeral=True)
        
        if interaction.user.id in self.boss_cog.disqualified:
            return await interaction.followup.send("You have been disqualified", ephemeral=True)
        
        if interaction.user.id in self.boss_cog.users:
            return await interaction.followup.send("You have already joined the boss", ephemeral=True)
        
        self.boss_cog.users.append(interaction.user.id)
        await interaction.followup.send("You have joined the Boss Battle!", ephemeral=True)
        await self.boss_cog._log_action(f"{interaction.user} has joined the {self.boss_cog.bossball} Boss Battle.")


# Django Models (integrated directly in cog)
class BossBattle(models.Model):
    """Active boss battle configuration"""
    ball_instance = models.OneToOneField(
        "bd_models.BallInstance",
        related_name="boss_battle",
        help_text="The ball instance acting as the boss",
        on_delete=models.CASCADE
    )
    max_hp = models.IntegerField(default=1000, help_text="Maximum HP of boss")
    current_hp = models.IntegerField(default=1000, help_text="Current HP of boss")
    attack_power = models.IntegerField(default=500, help_text="Attack power of the boss")
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True, help_text="Whether the boss battle is active")

    def __str__(self):
        return f"Boss Battle: {self.ball_instance.ball.country} ({self.current_hp}/{self.max_hp} HP)"


class BossBattleParticipant(models.Model):
    """Players participating in boss battles"""
    boss_battle = models.ForeignKey(
        BossBattle,
        related_name="participants",
        help_text="The boss battle this participant is in",
        on_delete=models.CASCADE
    )
    player = models.ForeignKey(
        "bd_models.Player",
        related_name="boss_battles",
        help_text="The player participating",
        on_delete=models.CASCADE
    )
    joined_at = models.DateTimeField(auto_now_add=True)
    total_damage_dealt = models.IntegerField(default=0, help_text="Total damage dealt by this participant")

    class Meta:
        unique_together = ['boss_battle', 'player']

    def __str__(self):
        return f"Player {self.player.discord_id} in {self.boss_battle}"


class BossBattleRound(models.Model):
    """Individual rounds in boss battles"""
    boss_battle = models.ForeignKey(
        BossBattle,
        related_name="rounds",
        help_text="The boss battle this round belongs to",
        on_delete=models.CASCADE
    )
    round = models.IntegerField(help_text="Round number")
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    is_attack_phase = models.BooleanField(default=True, help_text="Whether this is attack phase")
    boss_damage_taken = models.IntegerField(default=0, help_text="Total damage taken this round")

    class Meta:
        unique_together = ['boss_battle', 'round']
        ordering = ['round']

    def __str__(self):
        phase = "Attack" if self.is_attack_phase else "Defend"
        return f"Round {self.round} ({phase}) - {self.boss_battle}"


class BossRoundAction(models.Model):
    """Player actions in boss battle rounds"""
    round = models.ForeignKey(
        BossBattleRound,
        related_name="actions",
        help_text="The round this action belongs to",
        on_delete=models.CASCADE
    )
    participant = models.ForeignKey(
        BossBattleParticipant,
        related_name="actions",
        help_text="The participant who performed this action",
        on_delete=models.CASCADE
    )
    ball_used = models.ForeignKey(
        "bd_models.BallInstance",
        related_name="boss_actions",
        help_text="The ball instance used in this action",
        on_delete=models.CASCADE
    )
    damage_dealt = models.IntegerField(help_text="Damage dealt by this action")
    action_type = models.CharField(
        max_length=20,
        choices=[
            ('attack', 'Attack'),
            ('defend', 'Defend'),
            ('special', 'Special'),
        ],
        default='attack',
        help_text="Type of action performed"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Player {self.participant.player.discord_id} - {self.action_type} ({self.damage_dealt} dmg)"


class BossBattleReward(models.Model):
    """Rewards given for boss battles"""
    boss_battle = models.ForeignKey(
        BossBattle,
        related_name="rewards",
        help_text="The boss battle this reward is for",
        on_delete=models.CASCADE
    )
    winner = models.ForeignKey(
        "bd_models.Player",
        related_name="boss_wins",
        help_text="The player who won the boss battle",
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    reward_ball = models.ForeignKey(
        "bd_models.BallInstance",
        related_name="boss_reward_for",
        help_text="The ball instance given as reward",
        on_delete=models.CASCADE
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Reward for {self.boss_battle} - Winner: {self.winner}"


class BossCog(commands.GroupCog, name="boss"):
    """Boss battle system — fight powerful bosses with your collection!"""
    
    def __init__(self, bot: BallsDexBot):
        self.bot = bot
        # Boss battle state (from original BossPackageBD)
        self.boss_enabled = False
        self.bossball = None
        self.bossHP = 0
        self.bossmaxhp = 0
        self.users = []
        self.usersdamage = []  # Track damage per user
        self.usersinround = []
        self.balls = []  # Track selected balls to prevent duplicates
        self.round = 0  # Will be set to 1 when battle starts
        self.picking = False
        self.attack = True
        self.bossattack = 0
        self.disqualified = []  # Track disqualified users
        self.lasthitter = 0  # Track last person to hit boss
        self.currentvalue = ""  # Track round actions
        
        log.info("Boss Cog initialized")

    @app_commands.command(name="admin_start")
    @app_commands.describe(
        ball="The ball to use as boss",
        hp_amount="HP amount for the boss"
    )
    @checks.is_staff()
    async def admin_start(self, interaction: Interaction, ball: BallTransform, hp_amount: int):
        """Start a boss battle with the specified ball"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if self.boss_enabled:
            await interaction.followup.send("A boss battle is already active!", ephemeral=True)
            return
        
        # Ball is already provided by BallTransform
        
        try:
            # Follow original BossPackageBD pattern - only set HP and store ball
            self.bossball = ball
            self.bossHP = hp_amount
            self.bossmaxhp = hp_amount
            self.boss_enabled = True
            self.users = []
            self.usersinround = []
            self.balls = []  # Reset selected balls
            self.round = 0  # Start at 0 like original
            self.picking = False  # Don't start picking immediately
            self.attack = True
            
            await interaction.followup.send(f"Boss battle started with {ball.country}!", ephemeral=True)
            
            # Send announcement message with join button (like original)
            view = JoinButton(self)
            message = await interaction.channel.send(
                f"# The boss battle has begun! \n"
                f"## {ball.country}\n"
                f"**HP:** {self.bossHP}\n\n"
                f"Click the button below to join the fight!",
                view=view
            )
            view.message = message  # Store message reference
            
        except Exception as e:
            log.error(f"Error starting boss battle: {e}")
            await interaction.followup.send(f"Error starting boss battle: {e}", ephemeral=True)

    
    @app_commands.command()
    async def select(self, interaction: Interaction, ball: BallInstanceTransform):
        """Select countryball to use against the boss"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if [interaction.user.id, self.round] in self.usersinround:
            return await interaction.followup.send(
                f"You have already selected a ball", ephemeral=True
            )
        
        if not self.boss_enabled:
            return await interaction.followup.send("Boss is disabled", ephemeral=True)
        
        if not self.picking:
            return await interaction.followup.send(f"It is not yet time to select a ball", ephemeral=True)
        
        if interaction.user.id not in self.users:
            return await interaction.followup.send(
                "You did not join, or you're dead/disqualified.", ephemeral=True
            )
        
        if not ball.tradeable:
            await interaction.followup.send(
                f"You cannot use this ball.", ephemeral=True
            )
            return
        
        if ball in self.balls:
            return await interaction.followup.send(
                f"You cannot select the same ball twice", ephemeral=True
            )
        
        # Add to selected balls and track round participation
        self.balls.append(ball)
        self.usersinround.append([interaction.user.id, self.round])
        
        # Calculate stats with capping (like original)
        ball_attack = min(max(ball.attack, 0), MAX_STATS[0])
        ball_health = min(max(ball.health, 0), MAX_STATS[1])
        
        # Apply shiny buffs if applicable
        messageforuser = f"{ball.description(short=True, include_emoji=True, bot=self.bot)} has been selected for this round, with {ball_attack} ATK and {ball_health} HP"
        if ball.special and "✨" in messageforuser:
            messageforuser = f"{ball.description(short=True, include_emoji=True, bot=self.bot)} has been selected for this round, with {ball_attack}+{SHINY_BUFFS[0]} ATK and {ball_health}+{SHINY_BUFFS[1]} HP"
            ball_health += SHINY_BUFFS[1]
            ball_attack += SHINY_BUFFS[0]
        
        if not self.attack:  # Boss is defending, player attacks
            self.bossHP -= ball_attack
            self.usersdamage.append([interaction.user.id, ball_attack, ball.description(short=True, include_emoji=True, bot=self.bot)])
            self.currentvalue += f"{interaction.user}'s {ball.description(short=True, bot=self.bot)} has dealt {ball_attack} damage!\n"
            self.lasthitter = interaction.user.id
        else:  # Boss is attacking, player defends
            if self.bossattack >= ball_health:
                self.users.remove(interaction.user.id)
                self.currentvalue += f"{interaction.user}'s {ball.description(short=True, bot=self.bot)} had {ball_health}HP and died!\n"
            else:
                self.currentvalue += f"{interaction.user}'s {ball.description(short=True, bot=self.bot)} had {ball_health}HP and survived!\n"
        
        await interaction.followup.send(messageforuser, ephemeral=True)
        await self._log_action(f"-# Round {self.round}\n{interaction.user}'s {messageforuser}\n-# -------")

    
    @app_commands.command()
    async def ongoing(self, interaction: Interaction):
        """Show your damage to the boss in the current fight"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        user_damage = 0
        damage_details = ""
        
        for damage_record in self.usersdamage:
            if damage_record[0] == interaction.user.id:
                user_damage += damage_record[1]
                damage_details += f"{damage_record[2]}: {damage_record[1]} damage\n\n"
        
        if user_damage == 0:
            if interaction.user.id in self.users:
                await interaction.followup.send("You have not dealt any damage.", ephemeral=True)
            elif interaction.user.id in self.disqualified:
                await interaction.followup.send("You have been disqualified.", ephemeral=True)
            else:
                await interaction.followup.send("You have not joined the battle, or you have died.", ephemeral=True)
        else:
            if interaction.user.id in self.users:
                await interaction.followup.send(f"You have dealt {user_damage} total damage.\n\n{damage_details}", ephemeral=True)
            elif interaction.user.id in self.disqualified:
                await interaction.followup.send(f"You have dealt {user_damage} damage and have been disqualified.\n\n{damage_details}", ephemeral=True)
            else:
                await interaction.followup.send(f"You have dealt {user_damage} damage and you are now dead.\n\n{damage_details}", ephemeral=True)

    @app_commands.command(name="admin_conclude")
    @app_commands.choices(
        winner=[
            app_commands.Choice(name="Random", value="RNG"),
            app_commands.Choice(name="Most Damage", value="DMG"),
            app_commands.Choice(name="Last Hitter", value="LAST"),
            app_commands.Choice(name="No Winner", value="None"),
        ]
    )
    @checks.is_staff()
    async def conclude(self, interaction: Interaction, winner: str):
        """Finish the boss, conclude the Winner"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if not self.boss_enabled:
            return await interaction.followup.send("Boss is disabled.", ephemeral=True)
        
        if self.lasthitter not in self.users and winner == "LAST":
            return await interaction.followup.send(
                f"The last hitter is dead or disqualified.", ephemeral=True
            )
        
        self.picking = False
        self.boss_enabled = False
        
        # Calculate total damage per player (like original)
        damage_totals = []
        processed_users = []
        
        for damage_record in self.usersdamage:
            user_id = damage_record[0]
            if user_id not in processed_users:
                total_damage = sum(record[1] for record in self.usersdamage if record[0] == user_id)
                damage_totals.append([user_id, total_damage])
                processed_users.append(user_id)
        
        # Determine winner based on selection
        boss_winner = 0
        if winner == "DMG":
            if damage_totals:
                boss_winner = max(damage_totals, key=lambda x: x[1])[0]
        elif winner == "LAST":
            boss_winner = self.lasthitter
        elif winner == "RNG":
            if damage_totals:
                boss_winner = random.choice(damage_totals)[0]
        # "None" means no winner
        
        if boss_winner == 0 or winner == "None":
            await interaction.followup.send("Boss successfully concluded", ephemeral=True)
            await interaction.channel.send(f"# Boss has concluded\nThe boss has won the Boss Battle!")
            
            # Reset all battle state
            self._reset_battle_state()
            return
        
        # Reward the winner
        await self._reward_winner(boss_winner, channel=interaction.channel)
        await interaction.followup.send("Boss successfully concluded", ephemeral=True)
        
        # Reset battle state
        self._reset_battle_state()

    @app_commands.command(name="admin_end_round")
    @checks.is_staff()
    async def endround(self, interaction: Interaction):
        """End the current round"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if not self.boss_enabled:
            return await interaction.followup.send("Boss is disabled", ephemeral=True)
        
        if not self.picking:
            return await interaction.followup.send(
                "There are no ongoing rounds, use `/boss admin_attack` or `/boss admin_defend` to start one", ephemeral=True
            )
        
        self.picking = False
        
        # Handle round ending logic (exact original format)
        if not self.attack:  # Boss was defending
            if int(self.bossHP) <= 0:
                await interaction.channel.send(
                    f"# Round {self.round} has ended {self.bot.get_emoji(self.bossball.emoji_id) if self.bossball else ''}\nThere is 0 HP remaining on the boss, the boss has been defeated!"
                )
            else:
                await interaction.channel.send(
                    f"# Round {self.round} has ended {self.bot.get_emoji(self.bossball.emoji_id) if self.bossball else ''}\nThere is {self.bossHP} HP remaining on the boss"
                )
        else:  # Boss was attacking
            # Remove users who didn't select (exact original logic)
            snapshotusers = self.users.copy()
            for user_id in snapshotusers:
                if [user_id, self.round] not in self.usersinround:
                    user = await self.bot.fetch_user(int(user_id))
                    if str(user) not in self.currentvalue:
                        self.currentvalue += (str(user) + " has not selected on time and died!\n")
                        self.users.remove(user_id)
            
            if len(self.users) == 0:
                await interaction.channel.send(
                    f"# Round {self.round} has ended {self.bot.get_emoji(self.bossball.emoji_id) if self.bossball else ''}\nThe boss has dealt {self.bossattack} damage!\nThe boss has won!"
                )
            else:
                await interaction.channel.send(
                    f"# Round {self.round} has ended {self.bot.get_emoji(self.bossball.emoji_id) if self.bossball else ''}\nThe boss has dealt {self.bossattack} damage!\n"
                )
        
        # Send round stats (in-memory version)
        if self.currentvalue:
            await interaction.channel.send(f"**Round Stats:**\n{self.currentvalue}")
        
        # Clear round data but keep round number as is
        self.currentvalue = ""
        
        await interaction.followup.send("Round successfully ended", ephemeral=True)

    @app_commands.command(name="admin_attack")
    @app_commands.describe(attack_amount="Custom attack amount (optional)")
    @checks.is_staff()
    async def attack(self, interaction: Interaction, attack_amount: int | None = None):
        """Start a round where the Boss Attacks"""
        if not self.boss_enabled:
            return await interaction.followup.send("Boss is disabled", ephemeral=True)
        if self.picking:
            return await interaction.followup.send("There is already an ongoing round", ephemeral=True)
        if len(self.users) == 0:
            return await interaction.followup.send("There are not enough users to start the round", ephemeral=True)
        if self.bossHP <= 0:
            return await interaction.followup.send("The Boss is dead", ephemeral=True)
        
        self.round += 1
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        await interaction.followup.send("Round successfully started", ephemeral=True)
        await interaction.channel.send(
            f"Round {self.round}\n# {self.bossball.country} is preparing to attack!"
        )
        await interaction.channel.send(f"> Use `/boss select` to select your defending ball.\n> Your selected ball's HP will be used to defend.")
        
        self.picking = True
        self.attack = True
        self.bossattack = attack_amount if attack_amount is not None else random.randint(DAMAGE_RANGE[0], DAMAGE_RANGE[1])

    @app_commands.command(name="admin_defend")
    @checks.is_staff()
    async def defend(self, interaction: Interaction):
        """Start a round where the Boss Defends"""
        if not self.boss_enabled:
            return await interaction.followup.send("Boss is disabled", ephemeral=True)
        if self.picking:
            return await interaction.followup.send("There is already an ongoing round", ephemeral=True)
        if len(self.users) == 0:
            return await interaction.followup.send("There are not enough users to start the round", ephemeral=True)
        if self.bossHP <= 0:
            return await interaction.followup.send("The Boss is dead", ephemeral=True)
        
        self.round += 1
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        await interaction.followup.send("Round successfully started", ephemeral=True)
        await interaction.channel.send(
            f"Round {self.round}\n# {self.bossball.country} is preparing to defend!"
        )
        await interaction.channel.send(f"> Use `/boss select` to select your attacking ball.\n> Your selected ball's ATK will be used to attack.")
        
        self.picking = True
        self.attack = False

    @app_commands.command(name="admin_disqualify")
    @app_commands.describe(
        user="User to disqualify",
        user_id="User ID to disqualify", 
        undisqualify="Set to True to remove disqualification"
    )
    @checks.is_staff()
    async def disqualify(
        self,
        interaction: Interaction,
        user: discord.User | None = None,
        user_id: str | None = None,
        undisqualify: bool | None = False,
    ):
        """Disqualify a member from the boss"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if (user and user_id) or (not user and not user_id):
            await interaction.followup.send("You must provide either `user` or `user_id`.", ephemeral=True)
            return

        if not user:
            try:
                user = await self.bot.fetch_user(int(user_id))
            except ValueError:
                await interaction.followup.send("The user ID you gave is not valid.", ephemeral=True)
                return
            except discord.NotFound:
                await interaction.followup.send("The given user ID could not be found.", ephemeral=True)
                return
        else:
            user_id = str(user.id)

        if int(user_id) in self.disqualified:
            if undisqualify:
                self.disqualified.remove(int(user_id))
                await interaction.followup.send(
                    f"{user} has been removed from disqualification.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"{user} has already been disqualified.", ephemeral=True
                )
        elif undisqualify:
            await interaction.followup.send(f"{user} has **not** been disqualified yet.", ephemeral=True)
        elif not self.boss_enabled:
            self.disqualified.append(int(user_id))
            await interaction.followup.send(f"{user} will be disqualified from the next fight.", ephemeral=True)
        elif int(user_id) not in self.users:
            self.disqualified.append(int(user_id))
            await interaction.followup.send(f"{user} has been disqualified successfully.", ephemeral=True)
        else:
            self.users.remove(int(user_id))
            self.disqualified.append(int(user_id))
            await interaction.followup.send(f"{user} has been disqualified successfully.", ephemeral=True)

    @app_commands.command(name="admin_hackjoin")
    @app_commands.describe(
        user="User to force join",
        user_id="User ID to force join"
    )
    @checks.is_staff()
    async def hackjoin(
        self,
        interaction: Interaction,
        user: discord.User | None = None,
        user_id: str | None = None,
    ):
        """Force join a user to the boss battle"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if (user and user_id) or (not user and not user_id):
            await interaction.followup.send("You must provide either `user` or `user_id`.", ephemeral=True)
            return

        if not user:
            try:
                user = await self.bot.fetch_user(int(user_id))
            except ValueError:
                await interaction.followup.send("The user ID you gave is not valid.", ephemeral=True)
                return
            except discord.NotFound:
                await interaction.followup.send("The given user ID could not be found.", ephemeral=True)
                return
        else:
            user_id = str(user.id)

        if not self.boss_enabled:
            return await interaction.followup.send("Boss is disabled", ephemeral=True)
        if int(user_id) in self.users:
            return await interaction.followup.send("This user is already in the boss battle.", ephemeral=True)
        
        self.users.append(int(user_id))
        if int(user_id) in self.disqualified:
            self.disqualified.remove(int(user_id))
        
        await interaction.followup.send(f"{user} has been force-joined into the Boss Battle.", ephemeral=True)
        await self._log_action(f"{user} has joined the `{self.bossball}` Boss Battle. [hackjoin by {await self.bot.fetch_user(int(interaction.user.id))}]")
        
    @app_commands.command(name="admin_ping")
    @app_commands.describe(unselected="Only ping users who haven't selected yet")
    @checks.is_staff()
    async def ping(self, interaction: Interaction, unselected: bool | None = False):
        """Ping all the alive players"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if len(self.users) == 0:
            return await interaction.followup.send("There are no users joined/remaining", ephemeral=True)
        
        pingsmsg = ""
        if unselected:
            for userid in self.users:
                if [userid, self.round] not in self.usersinround:
                    pingsmsg += f"<@{userid}> "
        else:
            for userid in self.users:
                pingsmsg += f"<@{userid}> "
        
        if pingsmsg == "":
            await interaction.followup.send("All users have selected", ephemeral=True)
        elif len(pingsmsg) < 2000:
            await interaction.followup.send("Ping Successful", ephemeral=True)
            await interaction.channel.send(pingsmsg)
        else:
            await interaction.followup.send("Message too long, exceeds 2000 character limit", ephemeral=True)

    @app_commands.command(name="admin_stats")
    @checks.is_staff()
    async def stats(self, interaction: Interaction):
        """See current stats of the boss"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        stats_text = f"""Boss: {self.bossball.country if self.bossball else 'None'}
HP: {self.bossHP}/{self.bossmaxhp}
Round: {self.round}
Users: {len(self.users)}
Disqualified: {len(self.disqualified)}
Attack Phase: {self.attack}
Picking Active: {self.picking}
Users in Round: {len(self.usersinround)}
Damage Records: {len(self.usersdamage)}"""
        
        await interaction.followup.send(f"```\n{stats_text}\n```", ephemeral=True)

    async def _defeat_boss(self):
        """Handle boss defeat"""
        self.boss_enabled = False
        self.picking = False
        
        # Find winner (player with most participation)
        winner_id = None
        if self.users:
            winner_id = self.users[0]  # Simple: first participant wins
        
        if winner_id:
            await self._reward_winner(winner_id, channel=interaction.channel)

    async def _reward_winner(self, bosswinner: int, channel=None):
        """Reward the winner with a Boss special"""
        try:
            boss_special = None
            
            for special in specials.values():
                if special.name.lower() == "boss":
                    boss_special = special
                    break
            
            if boss_special:
                player = await Player.objects.aget(discord_id=bosswinner)
                # Create a special ball instance for the winner
                await BallInstance.objects.acreate(
                    ball=self.bossball,  # Use the actual boss ball
                    player=player,
                    special_id=boss_special.id,
                    attack_bonus=0,
                    health_bonus=0,
                    tradeable=True
                )
                bosswinner_user = await self.bot.fetch_user(int(bosswinner))
                await self._log_action(f"`BOSS REWARDS` gave {settings.collectible_name} {self.bossball} to {bosswinner_user}. "
                f"Special=Boss"
                f"ATK=0 HP=0")
                
                # Send announcement message (like original)
                if channel:
                    await channel.send(
                        f"# Boss has concluded {self.bot.get_emoji(self.bossball.emoji_id)}\n<@{bosswinner}> has won the Boss Battle!\n\n"
                        f"`Boss` `{self.bossball}` {settings.collectible_name} was successfully given.\n"
                    )
            else:
                if channel:
                    await channel.send("⚠️ Boss special not found! Please ensure there's a special named 'Boss' in the database.")
        except Exception as e:
            log.error(f"Error rewarding winner: {e}")
            if channel:
                await channel.send(f"❌ Error rewarding winner: {e}")

    def _reset_battle_state(self):
        """Reset all boss battle state variables"""
        self.round = 0
        self.balls = []
        self.users = []
        self.currentvalue = ""
        self.usersdamage = []
        self.usersinround = []
        self.bossHP = 0
        self.attack = False
        self.bossattack = 0
        self.bossball = None
        self.disqualified = []
        self.lasthitter = 0

    async def _log_action(self, message: str):
        """Log boss actions to console and webhook (BallsDex V3 pattern)"""
        log.info(f"Boss: {message}", extra={"webhook": True})
