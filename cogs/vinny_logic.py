import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import random
import re
import logging
import contextlib
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING
from google.genai import types
from cachetools import TTLCache

from utils import constants, api_clients
from utils.fact_extractor import extract_facts_from_message

# Helper Imports
from cogs.helpers import ai_classifiers, utilities, image_tasks, conversation_tasks

if TYPE_CHECKING:
    from main import VinnyBot

class VinnyLogic(commands.Cog):
    def __init__(self, bot: 'VinnyBot'):
        self.bot = bot
        # Safety settings are now in bot.GEMINI_TEXT_CONFIG, but we keep a local reference if needed
        self.memory_scheduler.start()
        self.status_rotator.start()
        self.channel_image_history = TTLCache(maxsize=100, ttl=600)
        
    def cog_unload(self):
        self.memory_scheduler.cancel()
        self.status_rotator.cancel()

    @tasks.loop(minutes=15)
    async def status_rotator(self):
        """Rotates Vinny's Discord status to add flavor."""
        await self.bot.wait_until_ready()
        
        activities = [
            discord.Game(name="with a lighter"),
            discord.Activity(type=discord.ActivityType.watching, name="paint dry"),
            discord.Activity(type=discord.ActivityType.listening, name="the voices"),
            discord.Activity(type=discord.ActivityType.competing, name="in a bar fight"),
            discord.Game(name="don't starve"),
            discord.Activity(type=discord.ActivityType.watching, name="you"),
            discord.Activity(type=discord.ActivityType.listening, name="sea shanties"),
        ]
        
        # Pick a random one
        new_activity = random.choice(activities)
        await self.bot.change_presence(activity=new_activity, status=discord.Status.online)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        logging.error(f"Error in command '{ctx.command}':", exc_info=error)
        if isinstance(error, commands.CommandNotFound): return
        elif isinstance(error, commands.MissingRequiredArgument): await ctx.send(f"eh, you missed somethin'. you need to provide the '{error.param.name}'.")
        elif isinstance(error, commands.MemberNotFound): await ctx.send(f"who? couldn't find anyone named '{error.argument}'.")
        elif isinstance(error, commands.BotMissingPermissions): await ctx.send("you're not the boss of me, but also i literally can't do that. check my permissions.")
        elif isinstance(error, commands.CommandOnCooldown): await ctx.send(f"whoa there, slow down. try again in {error.retry_after:.2f} seconds.")
        elif isinstance(error, commands.is_owner): await ctx.send("heh. nice try, pal.")
        else: await ctx.send("ah crap, my brain just shorted out. somethin' went wrong with that command.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        bot_names = ["vinny", "vincenzo", "vin vin"]
        if message.author.bot or message.id in self.bot.processed_message_ids or message.content.startswith(self.bot.command_prefix): return
        self.bot.processed_message_ids[message.id] = True
        
        try:
            if await utilities.check_and_fix_embeds(message):
                return
            
            if await ai_classifiers.is_a_correction(self.bot, message, self.bot.GEMINI_TEXT_CONFIG):
                return await conversation_tasks.handle_correction(self.bot, message)
            
            if message.reference and self.bot.user.mentioned_in(message):
                original_message = await message.channel.fetch_message(message.reference.message_id)
                if (original_message.attachments and "image" in original_message.attachments[0].content_type) or \
                   (original_message.embeds and original_message.embeds[0].image):
                    return await image_tasks.handle_image_reply(self.bot, message, original_message)
            
            if message.reference:
                ref_message = await message.channel.fetch_message(message.reference.message_id)
                if ref_message.author == self.bot.user:
                    await self.update_vinny_mood()
                    async with message.channel.typing():
                        await conversation_tasks.handle_direct_reply(self.bot, message)
                    return
                
            # Handles simple pings to the preceding image
            cleaned_content = re.sub(f'<@!?{self.bot.user.id}>', '', message.content).strip()
            
            if not cleaned_content and self.bot.user.mentioned_in(message): 
                async for last_message in message.channel.history(limit=1, before=message):
                    if last_message.attachments and "image" in last_message.attachments[0].content_type:
                        return await image_tasks.handle_image_reply(self.bot, message, last_message)
            
            # Autonomous and General Chat Logic
            should_respond, is_autonomous = False, False
            if self.bot.user.mentioned_in(message) or any(name in message.content.lower() for name in bot_names):
                should_respond = True
            elif self.bot.autonomous_mode_enabled and message.guild and random.random() < self.bot.autonomous_reply_chance:
                should_respond, is_autonomous = True, True
            elif message.guild is None:
                should_respond = True

            if should_respond:
                # --- START NEW LOGIC ---
                intent = None
                args = {}
                is_image_edit = False

                # 1. Check if replying to Vinny's image
                if message.reference:
                    try:
                        ref = await message.channel.fetch_message(message.reference.message_id)
                        if ref.author.id == self.bot.user.id and (ref.attachments or ref.embeds):
                            # 2. Ask Gatekeeper: Edit or Chat?
                            if await ai_classifiers.is_image_edit_request(self.bot, cleaned_content):
                                is_image_edit = True
                    except Exception:
                        pass

                if is_image_edit:
                    intent = "generate_image"
                    args = {"prompt": cleaned_content} # Treat the reply text as the prompt
                    logging.info(f"Gatekeeper: User {message.author} is editing an image.")
                else:
                    # 3. Normal Classifier (if not an edit)
                    intent, args = await ai_classifiers.get_intent_from_prompt(self.bot, message)
                # --- END NEW LOGIC ---

                typing_ctx = message.channel.typing() if not is_autonomous else contextlib.nullcontext()
                
                async with typing_ctx:
                    if intent == "generate_image":
                        prompt = args.get("prompt", "something, i guess. they didn't say what.")
                        
                        # 1. Retrieve the previous prompt for this channel
                        previous_prompt = self.channel_image_history.get(message.channel.id)
                        
                        # 2. Call the image task (now returns the NEW prompt used)
                        final_prompt = await image_tasks.handle_image_request(
                            self.bot, message, prompt, previous_prompt
                        )
                        
                        # 3. Save the new prompt to history
                        if final_prompt:
                            self.channel_image_history[message.channel.id] = final_prompt
                    
                    elif intent == "generate_user_portrait": 
                        await image_tasks.handle_paint_me_request(self.bot, message)
                    
                    elif intent == "get_user_knowledge":
                        target_user_name = args.get("target_user", "")
                        target_user = None

                        # 0. PRIORITY: Check for actual Discord Mentions first
                        # We filter out the bot itself (Vinny) from the mentions list
                        valid_mentions = [m for m in message.mentions if m.id != self.bot.user.id]
                        if valid_mentions:
                            target_user = valid_mentions[0]

                        # 1. If no mention, check for "me" / "myself" keywords
                        clean_target = target_user_name.lower().strip()
                        if not target_user and (not clean_target or clean_target in ["me", "myself", "i", "user", "the user", "self", "my profile"]):
                            target_user = message.author

                        # 2. If still no user, try searching by name (Text Search)
                        elif not target_user and message.guild:
                            # Sanitize: Remove '@' and extra spaces so search works
                            search_name = target_user_name.replace("@", "").strip()
                            
                            # A. Try finding by Discord Display Name
                            target_user = discord.utils.find(lambda m: search_name.lower() in m.display_name.lower(), message.guild.members)
                            
                            # B. If not found, try finding by Vinny's Internal Nickname
                            if not target_user:
                                target_user = await utilities.find_user_by_vinny_name(self.bot, message.guild, search_name)

                        # --- EXECUTE REQUEST ---
                        if target_user:
                            await conversation_tasks.handle_knowledge_request(self.bot, message, target_user)
                        else:
                            await message.channel.send(f"who? i looked all over, couldn't find anyone named '{target_user_name}'.")

                    elif intent == "tag_user":
                        user_to_tag = args.get("user_to_tag")
                        times = args.get("times_to_tag", 1)
                        if user_to_tag:
                            await conversation_tasks.find_and_tag_member(self.bot, message, user_to_tag, times)
                        else:
                            await message.channel.send("ya gotta tell me who to tag, pal.")
                    
                    elif intent == "get_my_name":
                         user_name_to_use = await self.bot.firestore_service.get_user_nickname(str(message.author.id)) or message.author.display_name
                         await message.channel.send(f"your name? i call ya '{user_name_to_use}'.")

                    else: 
                        async def update_sentiment_background():
                            try:
                                user_sentiment = await ai_classifiers.get_message_sentiment(self.bot, message.content)
                                sentiment_score_map = { 
                                    "positive": 2, 
                                    "flirty": 4,        
                                    "neutral": 1,       
                                    "sarcastic": 1,     
                                    "negative": -1,     
                                    "angry": -3         
                                }
                                score_change = sentiment_score_map.get(user_sentiment, 0)
                                
                                if message.guild:
                                    new_total_score = await self.bot.firestore_service.update_relationship_score(str(message.author.id), str(message.guild.id), score_change)
                                    await conversation_tasks.update_relationship_status(self.bot, str(message.author.id), str(message.guild.id), new_total_score)
                                
                                await self.update_mood_based_on_sentiment(user_sentiment)
                                await self.update_vinny_mood()
                            except Exception as e:
                                logging.error(f"Background sentiment update failed: {e}")

                        asyncio.create_task(update_sentiment_background())
                        
                        await conversation_tasks.handle_text_or_image_response(
                            self.bot, message, is_autonomous=is_autonomous, summary=None
                        )
                
                # Passive Learning
                if self.bot.PASSIVE_LEARNING_ENABLED:
                    image_bytes = None
                    mime_type = None
                    
                    if message.attachments:
                        for att in message.attachments:
                            if "image" in att.content_type and att.size < 8 * 1024 * 1024:
                                image_bytes = await att.read()
                                mime_type = att.content_type
                                break
                    
                    if extracted_facts := await extract_facts_from_message(
                        self.bot, 
                        message, 
                        author_name=None, 
                        image_bytes=image_bytes, 
                        mime_type=mime_type
                    ):
                        for key, value in extracted_facts.items():
                            await self.bot.firestore_service.save_user_profile_fact(
                                str(message.author.id), 
                                str(message.guild.id) if message.guild else None, 
                                key, 
                                value
                            )
                            logging.info(f"Learned visual fact for {message.author.display_name}: {key}={value}")
            else:
                explicit_reaction_keywords = ["react to this", "add an emoji", "emoji this", "react vinny"]
                if "pie" in message.content.lower() and random.random() < 0.75: await message.add_reaction('🥧')
                elif any(keyword in message.content.lower() for keyword in explicit_reaction_keywords) or (random.random() < self.bot.reaction_chance):
                    try:
                        emoji = random.choice(message.guild.emojis) if message.guild and message.guild.emojis else random.choice(['😂', '👍', '👀', '🍕', '🍻', '🥃', '🐶', '🎨'])
                        await message.add_reaction(emoji)
                    except discord.Forbidden: logging.warning(f"Missing permissions to add reactions in {message.channel.id}")
                    except Exception as e: logging.warning(f"Failed to add reaction: {e}")
                return

        except Exception:
            logging.critical("CRITICAL ERROR in on_message", exc_info=True)

    # --- MOOD LOGIC ---

    async def update_mood_based_on_sentiment(self, sentiment: str):
        """Influences Vinny's mood based on conversational sentiment."""
        mood_map = {
            "positive": ["cheerful", "flirty"],
            "negative": ["cranky", "depressed", "belligerent"],
            "sarcastic": ["suspicious", "cranky"],
            "flirty": ["flirty", "horny"],
            "angry": ["belligerent", "cranky"],
        }
        if sentiment in mood_map and random.random() < 0.25: 
            new_mood = random.choice(mood_map[sentiment])
            if self.bot.current_mood != new_mood:
                self.bot.current_mood = new_mood
                self.bot.last_mood_change_time = datetime.datetime.now()
                logging.info(f"Vinny's mood was influenced by conversation. New mood: {self.bot.current_mood}")

    async def update_vinny_mood(self):
        if datetime.datetime.now() - self.bot.last_mood_change_time > self.bot.MOOD_CHANGE_INTERVAL:
            self.bot.current_mood = random.choice([m for m in self.bot.MOODS if m != self.bot.current_mood])
            self.bot.last_mood_change_time = datetime.datetime.now()
            logging.info(f"Vinny's mood has changed to: {self.bot.current_mood}")

    @tasks.loop(minutes=30)
    async def memory_scheduler(self):
        await self.bot.wait_until_ready()
        logging.info("Memory scheduler starting...")
        for guild in self.bot.guilds:
            messages = []
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).read_message_history:
                    try:
                        since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=30)
                        async for message in channel.history(limit=100, after=since):
                            if not message.author.bot:
                                messages.append({"author": message.author.display_name, "content": message.content, "timestamp": message.created_at.isoformat()})
                    except discord.Forbidden: continue
                    except Exception as e: logging.error(f"Could not fetch history for channel '{channel.name}': {e}")
            if len(messages) > 5:
                logging.info(f"Generating summary for guild '{guild.name}' with {len(messages)} messages.")
                messages.sort(key=lambda x: x['timestamp'])
                if summary_data := await conversation_tasks.generate_memory_summary(self.bot, messages):
                    await self.bot.firestore_service.save_memory(str(guild.id), summary_data)
                    logging.info(f"Saved memory summary for guild '{guild.name}'.")
        logging.info("Memory scheduler finished.")

    # --- BOT COMMANDS ---
    
    @commands.command(name='help')
    async def help_command(self, ctx):
        embed = discord.Embed(title="What do ya want?", description="Heh. Aight, so you need help? Pathetic. Here's the stuff I can do if ya use the '!' thing. Don't get used to it.", color=discord.Color.dark_gold())
        embed.add_field(name="!vinnyknows [fact]", value="Teaches me somethin' about you. spill the beans.\n*Example: `!vinnyknows my favorite color is blue`*", inline=False)
        embed.add_field(name="!vibe [@user]", value="Checks what I think of you (or someone else if you tag 'em).", inline=False)
        embed.add_field(name="!forgetme", value="Makes me forget everything I know about you *in this server*.", inline=False)
        embed.add_field(name="!weather [location]", value="Gives you the damn weather. Don't blame me if it's wrong.\n*Example: `!weather 90210`*", inline=False)
        embed.add_field(name="!horoscope [sign]", value="I'll look at the sky and tell ya what's up. It's probably chaos.\n*Example: `!horoscope gemini`*", inline=False)
        embed.add_field(name="!propose [@user]", value="Get down on one knee and propose to someone special.", inline=False)
        embed.add_field(name="!marry [@user]", value="Accept a proposal from someone who just proposed to you.", inline=False)
        embed.add_field(name="!divorce", value="End your current marriage. Ouch.", inline=False)
        embed.add_field(name="!ballandchain", value="Checks who you're hitched to. If you have to ask, it might be bad news.", inline=False)
        embed.add_field(name="!vinnycalls [@user] [name]", value="Gives someone a nickname that I'll remember.\n*Example: `!vinnycalls @SomeUser Cori`*", inline=False)
        if await self.bot.is_owner(ctx.author):
            embed.add_field(name="!autonomy [on/off]", value="**(Owner Only)** Turns my brain on or off. Lets me talk without bein' talked to. Or shuts me up.", inline=False)
            embed.add_field(name="!set_relationship [@user] [score]", value="**(Owner Only)** Sets the numeric relationship score (-100 to 100).\n*Tiers: Nemesis, Enemy, Sketchy, Annoyance, Neutral, Chill, Friend, Bestie, Worshipped*", inline=False)
            embed.add_field(name="!forgive_all", value="**(Owner Only)** Resets EVERYONE'S relationship score to 0 (Neutral). Use this if I hate everyone.", inline=False)
            embed.add_field(name="!clear_memories", value="**(Owner Only)** Clears all of my automatic conversation summaries for this server.", inline=False)
        embed.set_footer(text="Now stop botherin' me. Salute!")
        await ctx.send(embed=embed)

    @commands.command(name='vinnycalls')
    async def vinnycalls_command(self, ctx, member: discord.Member, nickname: str):
        if await self.bot.firestore_service.save_user_nickname(str(member.id), nickname):
            await ctx.send(f"aight, got it. callin' {member.mention} '{nickname}'.")

    @commands.command(name='forgetme')
    async def forgetme_command(self, ctx):
        if not ctx.guild: return await ctx.send("this only works in a server, pal.")
        if await self.bot.firestore_service.delete_user_profile(str(ctx.author.id), str(ctx.guild.id)):
            await ctx.send(f"aight, {ctx.author.mention}. i scrambled my brains. who are you again?")
        else:
            await ctx.send("my head's already empty, pal.")

    @commands.command(name='propose')
    async def propose_command(self, ctx, member: discord.Member):
        if ctx.author == member: 
            return await ctx.send("heh, cute. but you can't propose to yourself, pal. pick someone else.")
        elif member == self.bot.user:
            return await ctx.send("whoa there, i'm flattered, really. but my heart belongs to the sea... and maybe rum. i'm off the market, sweetie.")
        elif member.bot: 
            return await ctx.send("proposin' to a robot? find a real pulse.")
        if await self.bot.firestore_service.save_proposal(str(ctx.author.id), str(member.id)):
            await ctx.send(f"whoa! {ctx.author.mention} is on one knee for {member.mention}. you got 5 mins to say yes with `!marry @{ctx.author.display_name}`.")

    @commands.command(name='marry')
    async def marry_command(self, ctx, member: discord.Member):
        if not await self.bot.firestore_service.check_proposal(str(member.id), str(ctx.author.id)):
            return await ctx.send(f"{member.display_name} didn't propose to you.")
        if await self.bot.firestore_service.finalize_marriage(str(member.id), str(ctx.author.id)):
            await ctx.send(f":tada: they said yes! i now pronounce {member.mention} and {ctx.author.mention} hitched!")

    @commands.command(name='divorce')
    async def divorce_command(self, ctx):
        profile = await self.bot.firestore_service.get_user_profile(str(ctx.author.id), None)
        if not profile or "married_to" not in profile: return await ctx.send("you ain't married.")
        partner_id = profile["married_to"]
        if await self.bot.firestore_service.process_divorce(str(ctx.author.id), partner_id):
            await ctx.send(f"it's over. {ctx.author.mention} has split from <@{partner_id}>. 📜")

    @commands.command(name='ballandchain')
    async def ballandchain_command(self, ctx):
        profile = await self.bot.firestore_service.get_user_profile(str(ctx.author.id), None)
        if profile and profile.get("married_to"):
            partner_id = profile.get("married_to")
            try:
                partner = await self.bot.fetch_user(int(partner_id))
                partner_name = partner.display_name if partner else "a ghost"
            except (discord.NotFound, ValueError):
                partner_name = "a ghost I can't find anymore"
            await ctx.send(f"you're shackled to **{partner_name}**. happened on **{profile.get('marriage_date', 'some forgotten day')}**.")
        else:
            await ctx.send("you ain't married to nobody.")

    @commands.command(name='weather')
    async def weather_command(self, ctx, *, location: str):
        async with ctx.typing():
            coords = await api_clients.geocode_location(self.bot.http_session, self.bot.OPENWEATHER_API_KEY, location)
            if not coords:
                return await ctx.send(f"eh, couldn't find that place '{location}'. you sure that's a real place?")
            current_weather_data = await api_clients.get_weather_data(self.bot.http_session, self.bot.OPENWEATHER_API_KEY, coords['lat'], coords['lon'])
            forecast_data = await api_clients.get_5_day_forecast(self.bot.http_session, self.bot.OPENWEATHER_API_KEY, coords['lat'], coords['lon'])

        if not current_weather_data:
            return await ctx.send("found the place but the damn current weather report is all garbled.")

        city_name = coords.get("name", "Unknown Location")
        embeds = []
        try:
            main_weather = current_weather_data["weather"][0]
            emoji = constants.get_weather_emoji(main_weather['main'])
            
            embed1 = discord.Embed(title=f"{emoji} Weather in {city_name}", description=f"**{main_weather.get('description', '').title()}**", color=discord.Color.blue())
            embed1.add_field(name="🌡️ Now", value=f"{current_weather_data['main'].get('temp'):.0f}°F", inline=True)
            embed1.add_field(name="🔼 High", value=f"{current_weather_data['main'].get('temp_max'):.0f}°F", inline=True)
            embed1.add_field(name="🔽 Low", value=f"{current_weather_data['main'].get('temp_min'):.0f}°F", inline=True)
            embed1.add_field(name="🤔 Feels Like", value=f"{current_weather_data['main'].get('feels_like'):.0f}°F", inline=True)
            embed1.add_field(name="💧 Humidity", value=f"{current_weather_data['main'].get('humidity')}%", inline=True)
            embed1.add_field(name="💨 Wind", value=f"{current_weather_data['wind'].get('speed'):.0f} mph", inline=True)
            embed1.add_field(name="📡 Live Radar", value=f"[Click to View](https://www.windy.com/{coords['lat']}/{coords['lon']})", inline=False)
            embed1.set_footer(text="Page 1 of 2 | don't blame me if the sky starts lyin'. salute!")
            embeds.append(embed1)
        except (KeyError, IndexError):
            return await ctx.send("failed to parse the current weather data. weird.")

        if forecast_data and forecast_data.get("list"):
            try:
                embed2 = discord.Embed(title=f"🗓️ 5-Day Forecast for {city_name}", color=discord.Color.dark_blue())
                daily_forecasts = {}
                for entry in forecast_data["list"]:
                    day = datetime.datetime.fromtimestamp(entry['dt']).strftime('%Y-%m-%d')
                    if day not in daily_forecasts: daily_forecasts[day] = {'highs': [], 'lows': [], 'icons': []}
                    daily_forecasts[day]['highs'].append(entry['main']['temp_max'])
                    daily_forecasts[day]['lows'].append(entry['main']['temp_min'])
                    daily_forecasts[day]['icons'].append(entry['weather'][0]['main'])
                
                day_keys = sorted(daily_forecasts.keys())
                for day in day_keys[:5]:
                    day_name = datetime.datetime.strptime(day, '%Y-%m-%d').strftime('%A')
                    high = max(daily_forecasts[day]['highs'])
                    low = min(daily_forecasts[day]['lows'])
                    most_common_icon = max(set(daily_forecasts[day]['icons']), key=daily_forecasts[day]['icons'].count)
                    emoji = constants.get_weather_emoji(most_common_icon)
                    embed2.add_field(name=f"**{day_name}**", value=f"{emoji} {high:.0f}°F / {low:.0f}°F", inline=False)
                embed2.set_footer(text="Page 2 of 2 | don't blame me if the sky starts lyin'. salute!")
                embeds.append(embed2)
            except Exception:
                logging.error("Failed to parse 5-day forecast data.", exc_info=True)

        class WeatherView(discord.ui.View):
            def __init__(self, embeds):
                super().__init__(timeout=60)
                self.embeds = embeds
                self.current_page = 0
                if len(self.embeds) < 2: self.children[1].disabled = True
            @discord.ui.button(label="Previous", style=discord.ButtonStyle.grey, disabled=True)
            async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.current_page -= 1
                await self.update_message(interaction)
            @discord.ui.button(label="Next", style=discord.ButtonStyle.blurple)
            async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.current_page += 1
                await self.update_message(interaction)
            async def update_message(self, interaction: discord.Interaction):
                self.children[0].disabled = self.current_page == 0
                self.children[1].disabled = self.current_page >= len(self.embeds) - 1
                await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

        if embeds: await ctx.send(embed=embeds[0], view=WeatherView(embeds))
        else: await ctx.send("somethin' went wrong with the damn weather machine.")

    @commands.command(name='horoscope')
    async def horoscope_command(self, ctx, *, sign: str):
        valid_signs = list(constants.SIGN_EMOJIS.keys())
        clean_sign = sign.lower()
        if clean_sign not in valid_signs: return await ctx.send(f"'{sign}'? that ain't a star sign, pal. try one of these: {', '.join(valid_signs)}.")
        async with ctx.typing():
            horoscope_data = await api_clients.get_horoscope(self.bot.http_session, clean_sign)
            if not horoscope_data: return await ctx.send("the stars are all fuzzy today. couldn't get a readin'. maybe they're drunk.")
            boring_horoscope = horoscope_data.get('horoscope_data', "The stars ain't talkin' today.")
            vinnyfied_text = boring_horoscope
            try:
                rewrite_prompt = (f"{self.bot.personality_instruction}\n\n# --- YOUR TASK ---\nYou must rewrite a boring horoscope into a chaotic, flirty, and slightly unhinged one in your own voice. The user's sign is **{clean_sign.title()}**. The boring horoscope is: \"{boring_horoscope}\"\n\n## INSTRUCTIONS:\nGenerate a short, single-paragraph monologue that gives you their horoscope in your unique, chaotic style. Do not just repeat the horoscope; interpret it with your personality.")
                response = await self.bot.make_tracked_api_call(model=self.bot.MODEL_NAME, contents=[rewrite_prompt], config=self.bot.GEMINI_TEXT_CONFIG)
                if response and response.text: vinnyfied_text = response.text.strip()
            except Exception: logging.error("Failed to Vinny-fy the horoscope.", exc_info=True)
            emoji = constants.SIGN_EMOJIS.get(clean_sign, "✨")
            embed = discord.Embed(title=f"{emoji} Horoscope for {clean_sign.title()}", description=vinnyfied_text, color=discord.Color.dark_purple())
            embed.set_thumbnail(url="https://i.imgur.com/4laks52.gif")
            embed.set_footer(text="don't blame me if the stars lie. they're drama queens.")
            embed.timestamp = datetime.datetime.now(ZoneInfo("America/New_York"))
            await ctx.send(embed=embed)

    @commands.command(name='vinnyknows')
    async def vinnyknows_command(self, ctx, *, knowledge_string: str):
        target_user = ctx.author
        if ctx.message.mentions:
            target_user = ctx.message.mentions[0]
            knowledge_string = re.sub(r'<@!?\d+>', '', knowledge_string).strip()

        extracted_facts = await extract_facts_from_message(self.bot, knowledge_string, author_name=target_user.display_name)
        if not extracted_facts:
            logging.warning(f"Fact extraction failed for string: '{knowledge_string}'")
            await ctx.send("eh? what're you tryin' to tell me? i didn't get that. try sayin' it like 'my favorite food is pizza'.")
            return

        saved_facts = []
        guild_id = str(ctx.guild.id) if ctx.guild else None
        for key, value in extracted_facts.items():
            if await self.bot.firestore_service.save_user_profile_fact(str(target_user.id), guild_id, key, value):
                saved_facts.append(f"'{key}' is '{value}'")

        if saved_facts:
            facts_confirmation = ", ".join(saved_facts)
            target_name = "themselves" if target_user == ctx.author else target_user.display_name
            confirmation_prompt = (
                f"{self.bot.personality_instruction}\n\n"
                f"# --- YOUR TASK ---\n"
                f"A user just taught you a fact. Your task is to confirm that you've learned it in your own chaotic, reluctant, or flirty way. Obey all your personality directives.\n\n"
                f"## CONTEXT:\n"
                f"- **The Teacher:** '{ctx.author.display_name}'\n"
                f"- **The Subject of the Fact:** '{target_name}'\n"
                f"- **The Fact Itself:** {facts_confirmation}\n\n"
                f"## INSTRUCTIONS:\n"
                f"1.  First, combine the **Subject** and the **Fact** into a complete thought (e.g., 'enraged smells like poo').\n"
                f"2.  Then, generate a short, lowercase, typo-ridden confirmation that shows you understand this complete thought. Acknowledge that **The Teacher** taught you this."
            )
            try:
                response = await self.bot.make_tracked_api_call(model=self.bot.MODEL_NAME, contents=[confirmation_prompt], config=self.bot.GEMINI_TEXT_CONFIG)
                if response and response.text: await ctx.send(response.text.strip())
                else: raise Exception("API call failed or returned no text.")
            except Exception:
                logging.error("Failed to generate dynamic confirmation for !vinnyknows.", exc_info=True)
                await ctx.send(f"aight, i got it. so {'your' if target_user == ctx.author else f'{target_user.display_name}\'s'} {facts_confirmation}. vinny will remember.")
        else:
            await ctx.send("my head's all fuzzy. tried to remember that but it slipped out.")

    @commands.command(name='autonomy')
    @commands.is_owner()
    async def autonomy_command(self, ctx, status: str):
        if status.lower() == 'on': self.bot.autonomous_mode_enabled = True; await ctx.send("aight, vinny's off the leash.")
        elif status.lower() == 'off': self.bot.autonomous_mode_enabled = False; await ctx.send("thank god. vinny's back in his cage.")
        else: await ctx.send("it's 'on' or 'off', pal. pick one.")

    @commands.command(name='set_relationship')
    @commands.is_owner()
    async def set_relationship_command(self, ctx, member: discord.Member, score: float):
        """Sets a user's relationship score directly. Status updates automatically."""
        guild_id = str(ctx.guild.id) if ctx.guild else None
        user_id = str(member.id)
        
        # 1. Update the score
        if await self.bot.firestore_service.save_user_profile_fact(user_id, guild_id, 'relationship_score', score):
            # 2. Force status update
            await conversation_tasks.update_relationship_status(self.bot, user_id, guild_id, score)
            
            # 3. Retrieve the new status to confirm to the user
            updated_profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id)
            new_status = updated_profile.get("relationship_status", "unknown")
            
            await ctx.send(f"aight. set {member.mention}'s score to **{score}**. they are now considered **'{new_status}'**.")
        else:
            await ctx.send("failed to save the score. my brain's broken.")

    @commands.command(name='clear_memories')
    @commands.is_owner()
    async def clear_memories_command(self, ctx):
        if not ctx.guild: return await ctx.send("can't clear memories from a private chat, pal.")
        path = constants.get_summaries_collection_path(self.bot.APP_ID, str(ctx.guild.id))
        if await self.bot.firestore_service.delete_docs(path): await ctx.send("aight, it's done. all the old chatter is gone.")
        else: await ctx.send("couldn't clear the memories. maybe they're stuck.")

    @commands.command(name='forgive_all')
    @commands.is_owner()
    async def forgive_all_command(self, ctx):
        """Resets the relationship score of EVERY user in the server to 0."""
        if not ctx.guild: return await ctx.send("Server only, pal.")
        
        await ctx.send("Aight, hold on. I'm wiping the slate clean...")
        async with ctx.typing():
            # 1. Get all user IDs
            user_ids = await self.bot.firestore_service.get_all_user_ids_in_guild(str(ctx.guild.id))
            
            if not user_ids:
                return await ctx.send("I don't know anyone here yet. Job done.")

            count = 0
            # 2. Reset them one by one
            for user_id in user_ids:
                # Reset score to 0
                await self.bot.firestore_service.save_user_profile_fact(user_id, str(ctx.guild.id), "relationship_score", 0)
                # Reset status to 'neutral'
                await self.bot.firestore_service.save_user_profile_fact(user_id, str(ctx.guild.id), "relationship_status", "neutral")
                count += 1
                
        await ctx.send(f"Done. I forgave {count} people. You're all 'neutral' to me now. Don't make me regret it.")

    @commands.command(name='vibe')
    async def vibe_command(self, ctx, member: discord.Member = None):
        """
        Checks Vinny's opinion of you using the NEW 9-Tier System.
        Usage: !vibe OR !vibe @User
        """
        target_user = member or ctx.author
        user_id = str(target_user.id)
        guild_id = str(ctx.guild.id) if ctx.guild else None
        
        # 1. Get Profile Data
        profile = await self.bot.firestore_service.get_user_profile(user_id, guild_id)
        if not profile:
            if target_user == ctx.author:
                return await ctx.send("i don't even know who you are yet.")
            else:
                return await ctx.send(f"i don't know who {target_user.display_name} is. never met 'em.")

        rel_score = profile.get("relationship_score", 0)
        
        # 2. FORCE RE-CALCULATE STATUS (To fix old "Distrusted" labels)
        if rel_score >= 90: rel_status = "worshipped"
        elif rel_score >= 60: rel_status = "bestie"
        elif rel_score >= 25: rel_status = "friend"
        elif rel_score >= 10: rel_status = "chill"
        elif rel_score >= -10: rel_status = "neutral"
        elif rel_score >= -25: rel_status = "annoyance"
        elif rel_score >= -60: rel_status = "sketchy"
        elif rel_score >= -90: rel_status = "enemy"
        else: rel_status = "nemesis"

        # 3. Get Mood & Color
        mood = self.bot.current_mood
        
        color_map = {
            "worshipped": discord.Color.gold(),
            "bestie":     discord.Color.purple(),
            "friend":     discord.Color.green(),
            "chill":      discord.Color.teal(),
            "neutral":    discord.Color.dark_magenta(),
            "annoyance":  discord.Color.orange(),
            "sketchy":    discord.Color.dark_orange(),
            "enemy":      discord.Color.red(),
            "nemesis":    discord.Color.dark_red()
        }
        embed_color = color_map.get(rel_status, discord.Color.dark_magenta())

        # 4. Generate Comment
        prompt = (
            f"{self.bot.personality_instruction}\n\n"
            f"# TASK:\n"
            f"The user '{ctx.author.display_name}' is checking your opinion of '{target_user.display_name}'.\n"
            f"- Your Current Mood: {mood}\n"
            f"- Your Opinion of {target_user.display_name}: {rel_status.upper()} (Score: {rel_score:.1f})\n\n"
            f"Write a short, one-sentence response telling {ctx.author.display_name} exactly what you think of {target_user.display_name}."
        )
        
        async with ctx.typing():
            try:
                response = await self.bot.make_tracked_api_call(
                    model=self.bot.MODEL_NAME, 
                    contents=[prompt], 
                    config=self.bot.GEMINI_TEXT_CONFIG
                )
                comment = response.text.strip() if response else "i don't even know what i'm feelin right now."
            except:
                comment = "my brain's fried."

        # 5. Send Embed
        embed = discord.Embed(title=f"Vibe Check: {target_user.display_name}", color=embed_color)
        embed.add_field(name="🧠 Current Mood", value=mood.title(), inline=True)
        # Use .title() so "sketchy" becomes "Sketchy"
        embed.add_field(name="❤️ Relationship", value=f"{rel_status.title()} ({rel_score:.0f})", inline=True)
        embed.add_field(name="💬 Vinny says:", value=comment, inline=False)
        
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(VinnyLogic(bot))