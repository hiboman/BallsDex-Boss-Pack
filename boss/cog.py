from __future__ import annotations

import io
import logging
import random
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from django.db import models
from django.utils import timezone

from bd_models.models import Ball, BallInstance, Player
from bd_models.models import balls as balls_cache
from bd_models.models import specials
from ballsdex.core.utils.transformers import BallTransform, BallInstanceTransform, BallEnabledTransform
from ballsdex.core.utils import checks
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.boss")
Interaction = discord.Interaction["BallsDexBot"]

# Configuration constants
SHINYBUFFS = [2000,2000] # Shiny Buffs
# ATK, HP
MAXSTATS = [10000,10000] # Max stats a card is limited to (before buffs)
# ATK, HP
DAMAGERNG = [0,2000] # Damage a boss can deal IF attack_amount has NOT been inputted in /boss admin attack.
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
            
        if self.boss_cog.round > 0 or self.boss_cog.picking:
            return await interaction.followup.send("The boss battle has already started", ephemeral=True)
        
        self.boss_cog.users.append(interaction.user.id)
        await interaction.followup.send("You have joined the Boss Battle!", ephemeral=True)
        await self.boss_cog._log_action(f"{interaction.user} has joined the {self.boss_cog.bossball} Boss Battle.")


# Django Models (integrated directly in cog)
class BossBattle(models.Model):
    """Active boss battle configuration"""
    ball_instance = models.OneToOneField(
        "bd_models.BallInstance",
        related_name="+",
        help_text="The ball instance acting as the boss",
        on_delete=models.DO_NOTHING,
        db_constraint=False
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
        related_name="+",
        help_text="The player participating",
        on_delete=models.DO_NOTHING,
        db_constraint=False
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
        related_name="+",
        help_text="The ball instance used in this action",
        on_delete=models.DO_NOTHING,
        db_constraint=False
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
        related_name="+",
        help_text="The player who won the boss battle",
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        null=True,
        blank=True
    )
    reward_ball = models.ForeignKey(
        "bd_models.BallInstance",
        related_name="+",
        help_text="The ball instance given as reward",
        on_delete=models.DO_NOTHING,
        db_constraint=False
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Reward for {self.boss_battle} - Winner: {self.winner}"


class Boss(commands.GroupCog, name="boss"):
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
        self.pending_selections: dict[int, BallInstance] = {}
        
        log.info("Boss Cog initialized")
        
    admin = app_commands.Group(name="admin", description="Boss administration commands")

    def admin_permissions_check():
        """Custom permission check for admin commands that works with interactions"""
        async def check(interaction: discord.Interaction["BallsDexBot"]) -> bool:
            from users.utils import get_user_model
            
            try:
                user_model = get_user_model()
                dj_user = await user_model.objects.filter(discord_id=interaction.user.id).aget()
                if not dj_user.is_active:
                    return False
                return await dj_user.ahas_perms(["bd_models.add_ballinstance"])
            except user_model.DoesNotExist:
                return False
        return app_commands.check(check)

    @admin.command()
    @admin_permissions_check()
    async def start(self, interaction: discord.Interaction["BallsDexBot"], ball: BallEnabledTransform, hp_amount: int):
        """
        Start a boss battle with the specified ball
        
        Parameters
        ----------
        ball: Ball
            The ball to use as boss
        hp_amount: int
            HP amount for the boss
        """
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
            self.pending_selections = {}
            
            await interaction.followup.send(f"Boss battle started with {ball.country}!", ephemeral=True)
            
            # Prepare boss image file
            extension = ball.collection_card.name.split(".")[-1]
            file_location = str(ball.collection_card.path)
            file = discord.File(file_location, filename=f"boss.{extension}")
            
            # Send announcement message with join button and boss image
            view = JoinButton(self)
            message = await interaction.channel.send(
                f"# The boss battle has begun! {self.bot.get_emoji(ball.emoji_id)}\n"
                f"-# HP: {self.bossHP}",
                file=file,
                view=view
            )
            view.message = message  # Store message reference
            
        except Exception as e:
            log.error(f"Error starting boss battle: {e}")
            await interaction.followup.send(f"Error starting boss battle: {e}", ephemeral=True)

    
    @app_commands.command()
    async def select(self, interaction: Interaction, ball: BallInstanceTransform):
        """
        Select countryball to use against the boss
        
        Parameters
        ----------
        ball: Ball
            The ball to use for this round
        """
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
        
        # Store the selection for processing in endround
        self.pending_selections[interaction.user.id] = ball
        
        # Add to selected balls and track round participation
        self.balls.append(ball)
        self.usersinround.append([interaction.user.id, self.round])
        
        # Calculate stats with capping (like original)
        ball_attack = min(max(ball.attack, 0), MAXSTATS[0])
        ball_health = min(max(ball.health, 0), MAXSTATS[1])
        
        # Apply shiny buffs if applicable
        messageforuser = f"{ball.description(short=True, include_emoji=True, bot=self.bot)} has been selected for this round, with {ball_attack} ATK and {ball_health} HP"
        if ball.special_id and "✨" in messageforuser:
            messageforuser = f"{ball.description(short=True, include_emoji=True, bot=self.bot)} has been selected for this round, with {ball_attack}+{SHINYBUFFS[0]} ATK and {ball_health}+{SHINYBUFFS[1]} HP"
        
        await interaction.followup.send(messageforuser, ephemeral=True)
        await self._log_action(f"-# Round {self.round}\n{interaction.user}'s {messageforuser}\n-# -------")

    
    @app_commands.command()
    async def ongoing(self, interaction: Interaction):
        """
        Show your damage to the boss in the current fight
        """
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

    @admin.command()
    @admin_permissions_check()
    @app_commands.choices(
        winner=[
            app_commands.Choice(name="Random", value="RNG"),
            app_commands.Choice(name="Most Damage", value="DMG"),
            app_commands.Choice(name="Last Hitter", value="LAST"),
            app_commands.Choice(name="No Winner", value="None"),
        ]
    )
    async def conclude(self, interaction: discord.Interaction["BallsDexBot"], winner: str):
        """
        Finish the boss, conclude the Winner
        
        Parameters
        ----------
        winner: app_commands.Choice[str]
            Winner selection method (RNG/DMG/LAST/None)
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if not self.boss_enabled:
            return await interaction.followup.send("Boss is disabled.", ephemeral=True)
        
        if self.lasthitter not in self.users and winner == "LAST":
            return await interaction.followup.send(
                f"The last hitter is dead or disqualified.", ephemeral=True
            )
        
        self.picking = False
        self.boss_enabled = False
        
        # Calculate total damage per player (following inspirational code pattern)
        test = self.usersdamage
        test2 = []
        total = ""
        total2 = ""
        totalnum = []
        
        for i in range(len(test)):
            if test[i][0] not in test2:
                temp = 0
                tempvalue = test[i][0]
                test2.append(tempvalue)
                for j in range(len(test)):
                    if test[j][0] == tempvalue:
                        temp += test[j][1]
                if test[i][0] in self.users:
                    user = await self.bot.fetch_user(int(tempvalue))
                    total += f"{user} has dealt a total of {temp} damage!\n"
                    totalnum.append([tempvalue, temp])
                else:
                    user = await self.bot.fetch_user(int(tempvalue))
                    total2 += f"[Dead/Disqualified] {user} has dealt a total of {temp} damage!\n"
        
        # Determine winner based on selection
        bosswinner = 0
        highest = 0
        if winner == "DMG":
            for k in range(len(totalnum)):
                if totalnum[k][1] > highest:
                    highest = totalnum[k][1]
                    bosswinner = totalnum[k][0]
        elif winner == "LAST":
            bosswinner = self.lasthitter
        elif winner == "RNG":
            if len(totalnum) != 0:
                bosswinner = totalnum[random.randint(0, len(totalnum)-1)][0]
        
        # Create totalstats.txt file
        stats_content = f"{total}{total2}"
        stats_file = discord.File(
            fp=io.StringIO(stats_content),
            filename="totalstats.txt"
        )
        
        if bosswinner == 0 or winner == "None":
            await interaction.followup.send("Boss successfully concluded", ephemeral=True)
            await interaction.channel.send(f"# Boss has concluded {self.bot.get_emoji(self.bossball.emoji_id) if self.bossball else ''}\nThe boss has won the Boss Battle!")
            await interaction.channel.send(file=stats_file)
            
            # Reset all battle state
            self._reset_battle_state()
            return
        
        # Reward the winner
        await self._reward_winner(bosswinner, channel=interaction.channel)
        await interaction.followup.send("Boss successfully concluded", ephemeral=True)
        await interaction.channel.send(file=stats_file)
        
        # Reset battle state
        self._reset_battle_state()

    @admin.command()
    @admin_permissions_check()
    async def endround(self, interaction: discord.Interaction["BallsDexBot"]):
        """End the current round"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if not self.boss_enabled:
            return await interaction.followup.send("Boss is disabled", ephemeral=True)
        
        if not self.picking:
            return await interaction.followup.send(
                "There are no ongoing rounds, use `/boss admin_attack` or `/boss admin_defend` to start one", ephemeral=True
            )
        
        self.picking = False
        
        # Process selections and verify they still exist/aren't deleted
        for user_id, ball in list(self.pending_selections.items()):
            # Verification check
            if not await BallInstance.objects.filter(pk=ball.pk, deleted=False).aexists():
                # Ball was deleted or soft-deleted!
                # Remove from battle memory as if it never existed
                if ball in self.balls:
                    self.balls.remove(ball)
                if [user_id, self.round] in self.usersinround:
                    self.usersinround.remove([user_id, self.round])
                del self.pending_selections[user_id]
                continue
            
            # Ball exists, process results
            ball_attack = min(max(ball.attack, 0), MAXSTATS[0])
            ball_health = min(max(ball.health, 0), MAXSTATS[1])
            
            # Re-check shiny buffs for logic
            ball_desc = ball.description(short=True, include_emoji=True, bot=self.bot)
            if ball.special_id and "✨" in ball_desc:
                ball_health += SHINYBUFFS[1]
                ball_attack += SHINYBUFFS[0]
            
            if not self.attack:  # Boss is defending, players attack
                self.bossHP -= ball_attack
                self.usersdamage.append([user_id, ball_attack, ball_desc])
                self.currentvalue += f"{await self.bot.fetch_user(user_id)}'s {ball.description(short=True, bot=self.bot)} has dealt {ball_attack} damage!\n"
                self.lasthitter = user_id
            else:  # Boss is attacking, players defend
                user_obj = await self.bot.fetch_user(user_id)
                if self.bossattack >= ball_health:
                    if user_id in self.users:
                        self.users.remove(user_id)
                    self.currentvalue += f"{user_obj}'s {ball.description(short=True, bot=self.bot)} had {ball_health}HP and died!\n"
                else:
                    self.currentvalue += f"{user_obj}'s {ball.description(short=True, bot=self.bot)} had {ball_health}HP and survived!\n"

        # Clear pending selections for next round
        self.pending_selections = {}
        
        # Remove users who didn't select (applies to both attack and defend phases)
        # This will also catch users whose balls were deleted above
        snapshotusers = self.users.copy()
        for user_id in snapshotusers:
            if [user_id, self.round] not in self.usersinround:
                user = await self.bot.fetch_user(int(user_id))
                if str(user) not in self.currentvalue:
                    self.currentvalue += (str(user) + " has not selected on time and died!\n")
                    self.users.remove(user_id)
        
        # Handle round ending logic
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
            if len(self.users) == 0:
                await interaction.channel.send(
                    f"# Round {self.round} has ended {self.bot.get_emoji(self.bossball.emoji_id) if self.bossball else ''}\nThe boss has dealt {self.bossattack} damage!\nThe boss has won!"
                )
            else:
                await interaction.channel.send(
                    f"# Round {self.round} has ended {self.bot.get_emoji(self.bossball.emoji_id) if self.bossball else ''}\nThe boss has dealt {self.bossattack} damage!\n"
                )
        
        # Send round stats as file
        if self.currentvalue:
            # Create roundstats.txt file
            stats_content = self.currentvalue
            stats_file = discord.File(
                fp=io.StringIO(stats_content),
                filename="roundstats.txt"
            )
            await interaction.channel.send(file=stats_file)
        
        # Clear round data but keep round number as is
        self.currentvalue = ""
        
        await interaction.followup.send("Round successfully ended", ephemeral=True)

    @admin.command()
    @admin_permissions_check()
    async def attack(self, interaction: discord.Interaction["BallsDexBot"], attack_amount: int | None = None):
        """
        Start a round where the Boss Attacks
        
        Parameters
        ----------
        attack_amount: int
            Custom attack amount
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if not self.boss_enabled:
            return await interaction.followup.send("Boss is disabled", ephemeral=True)
        if self.picking:
            return await interaction.followup.send("There is already an ongoing round", ephemeral=True)
        if len(self.users) == 0:
            return await interaction.followup.send("There are not enough users to start the round", ephemeral=True)
        if self.bossHP <= 0:
            return await interaction.followup.send("The Boss is dead", ephemeral=True)
        
        self.round += 1
        
        await interaction.followup.send("Round successfully started", ephemeral=True)
        
        # Prepare boss image file for attack phase
        extension = self.bossball.wild_card.name.split(".")[-1]
        file_location = str(self.bossball.wild_card.path)
        file = discord.File(file_location, filename=f"boss.{extension}")
        
        await interaction.channel.send(
            f"Round {self.round}\n# {self.bossball.country} is preparing to attack! {self.bot.get_emoji(self.bossball.emoji_id)}",
            file=file
        )
        await interaction.channel.send(f"> Use `/boss select` to select your defending ball.\n> Your selected ball's HP will be used to defend.")
        
        self.picking = True
        self.attack = True
        self.bossattack = attack_amount if attack_amount is not None else random.randint(DAMAGERNG[0], DAMAGERNG[1])

    @admin.command()
    @admin_permissions_check()
    async def defend(self, interaction: discord.Interaction["BallsDexBot"]):
        """Start a round where the Boss Defends"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        if not self.boss_enabled:
            return await interaction.followup.send("Boss is disabled", ephemeral=True)
        if self.picking:
            return await interaction.followup.send("There is already an ongoing round", ephemeral=True)
        if len(self.users) == 0:
            return await interaction.followup.send("There are not enough users to start the round", ephemeral=True)
        if self.bossHP <= 0:
            return await interaction.followup.send("The Boss is dead", ephemeral=True)
        
        self.round += 1
        
        await interaction.followup.send("Round successfully started", ephemeral=True)
        
        # Prepare boss image file for defend phase
        extension = self.bossball.wild_card.name.split(".")[-1]
        file_location = str(self.bossball.wild_card.path)
        file = discord.File(file_location, filename=f"boss.{extension}")
        
        await interaction.channel.send(
            f"Round {self.round}\n# {self.bossball.country} is preparing to defend! {self.bot.get_emoji(self.bossball.emoji_id)}",
            file=file
        )
        await interaction.channel.send(f"> Use `/boss select` to select your attacking ball.\n> Your selected ball's ATK will be used to attack.")
        
        self.picking = True
        self.attack = False

    @admin.command()
    @admin_permissions_check()
    async def disqualify(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User | None = None,
        user_id: str | None = None,
        undisqualify: bool | None = False,
    ):
        """
        Disqualify or undisqualify a user from the boss battle
        
        Parameters
        ----------
        user: discord.User
            User to disqualify
        user_id: str
            User ID to disqualify
        undisqualify: bool
            Set to True to remove disqualification
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

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

    @admin.command()
    @admin_permissions_check()
    async def hackjoin(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User | None = None,
        user_id: str | None = None,
    ):
        """
        Force join a user to the boss battle
        
        Parameters
        ----------
        user: discord.User
            User to force join
        user_id: str
            User ID to force join
        """
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
        
    @admin.command()
    @admin_permissions_check()
    async def ping(self, interaction: discord.Interaction["BallsDexBot"], unselected: bool | None = False):
        """
        Ping all the alive players
        
        Parameters
        ----------
        unselected: bool
            Only ping users who haven't selected yet
        """
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

    @admin.command()
    @admin_permissions_check()
    async def stats(self, interaction: discord.Interaction["BallsDexBot"]):
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
                f"Special=Boss "
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
        self.pending_selections = {}

    async def _log_action(self, message: str):
        """Log boss actions to console and webhook (BallsDex V3 pattern)"""
        log.info(f"Boss: {message}", extra={"webhook": True})
